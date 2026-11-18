"""Results page: sidebar (folders / types), list or gallery of found files, filters, preview."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

from PyQt6.QtCore import (QAbstractTableModel, QModelIndex, QRunnable, QSize, QSortFilterProxyModel,
                          Qt, QThreadPool, QTimer, pyqtSignal)
from PyQt6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
                             QPushButton, QSplitter, QStackedWidget, QTableView, QTabWidget, QToolButton,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import theme
from .thumbs import READ_LIMIT, THUMB, ThumbLoader
from .widgets import Notice, clear_layout, dot_icon, help_button, label
from ..core.models import CATEGORIES, Condition, RecoverableItem, Source, human_size
from ..core.query import compile_query

SURVIVOR_SOURCES = {Source.SNAPSHOT, Source.THUMBNAIL, Source.AUTOSAVE}
COLUMNS = ["Name", "Folder", "Size", "Modified", "Recovery chance", "Found by"]
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ITEM_ROLE = Qt.ItemDataRole.UserRole + 2
CHANCE_ORDER = {"Excellent": 0, "Good": 1, "Poor": 2, "Unlikely": 3}

CHANCE_HELP = {
    "Excellent": "Its disk space hasn't been reused. It should come back complete.",
    "Good": "Found by its contents and its structure checks out. Usually opens fine.",
    "Poor": "Some of its space now holds other data, so parts may be damaged.",
    "Unlikely": "Another file now uses all of its space. What you'd get back is that other file's data.",
}


def chance_help(it: RecoverableItem) -> str:
    if it.condition == Condition.ERASED and it.deleted and it.file_path is None:
        return ("The drive already erased this file's data (on SSDs this is TRIM), so it reads back as zeros. "
                "Only its name survives. Try \"Find surviving copies\" for a restore point, preview or autosave.")
    if it.condition == Condition.PARTLY_ERASED:
        return "Part of this file was erased by the drive (SSD TRIM) and reads back as zeros. The rest may still open."
    return CHANCE_HELP.get(it.chance, "")

SEARCH_HELP = """<b>Search tips</b> (combine freely):<br>
<code>holiday 2023</code> — words in the name or folder<br>
<code>*.docx</code> &nbsp; <code>IMG_*</code> — wildcards on the name<br>
<code>ext:jpg,png</code> &nbsp; <code>type:photos</code> — kinds of file<br>
<code>size:&gt;5mb</code> &nbsp; <code>size:100kb..2mb</code><br>
<code>modified:2024</code> &nbsp; <code>modified:&gt;2024-03-01</code><br>
<code>in:Documents</code> — inside a folder<br>
<code>-backup</code> — leave out matches<br>
<code>"exact phrase"</code> &nbsp; <code>re:^IMG_\\d+</code>"""

_CAT_COLORS = {"Images": "#2E7A9E", "Documents": "#2D5FA8", "Video": "#7B4FA3", "Audio": "#B8572B",
               "Archives": "#7A6A2E", "Code & data": "#3F7F5F", "Other": "#6B7478"}


def type_icon(ext: str, category: str, size: int) -> QIcon:
    """A small document glyph with the extension on it, coloured by category."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w = size * 0.74
    x = (size - w) / 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(_CAT_COLORS.get(category, "#6B7478")))
    p.drawRoundedRect(int(x), int(size * 0.06), int(w), int(size * 0.88), size * 0.1, size * 0.1)
    if size >= 32:
        f = QFont()
        f.setBold(True)
        f.setPixelSize(max(8, int(size * 0.2)))
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, (ext or "?").upper()[:5])
    p.end()
    return QIcon(pm)


class FileModel(QAbstractTableModel):
    checkedChanged = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.items: List[RecoverableItem] = []
        self.checked: Set[int] = set()
        self.row_of: Dict[int, int] = {}
        self.gallery = False
        self.thumbs = ThumbLoader()
        self.thumbs.ready.connect(self._thumb_ready)
        self.read_fn: Optional[Callable[[RecoverableItem, int], bytes]] = None
        self._icons: Dict[tuple, QIcon] = {}
        self._dots: Dict[str, QIcon] = {}
        self._thumb_icons: Dict[int, QIcon] = {}
        self.restyle()

    def restyle(self):
        self._dots = {k: dot_icon(theme.chance_color(k)) for k in CHANCE_ORDER}

    def set_items(self, items: List[RecoverableItem], read_fn):
        self.beginResetModel()
        self.items = items
        self.row_of = {it.id: r for r, it in enumerate(items)}
        self.checked = set()
        self.read_fn = read_fn
        self.thumbs.reset()
        self._thumb_icons.clear()
        self.endResetModel()
        self.checkedChanged.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        return f

    def _icon(self, it: RecoverableItem, size: int) -> QIcon:
        key = (it.ext, size)
        ic = self._icons.get(key)
        if ic is None:
            ic = self._icons[key] = type_icon(it.ext, it.category, size)
        return ic

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return it.name
            if col == 1:
                return it.folder or "(top folder)"
            if col == 2:
                return human_size(it.size)
            if col == 3:
                return it.modified.strftime("%Y-%m-%d %H:%M") if it.modified else ""
            if col == 4:
                return it.chance if it.deleted else "Still exists"
            if col == 5:
                return it.source
        if role == SORT_ROLE:
            return [it.name.lower(), it.folder.lower(), it.size,
                    it.modified.timestamp() if it.modified and it.modified.year > 1970 else 0,
                    CHANCE_ORDER.get(it.chance, 9), it.source][col]
        if role == ITEM_ROLE:
            return it
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if it.id in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole:
            if col == 4:
                return self._dots.get(it.chance)
            if col == 0:
                if self.gallery:
                    return self._gallery_icon(it)
                return self._icon(it, 16)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 4:
                return chance_help(it)
            return it.full_path + (f"\n{it.note}" if it.note else "")
        return None

    def _gallery_icon(self, it: RecoverableItem) -> QIcon:
        if it.category != "Images" or self.read_fn is None:
            return self._icon(it, THUMB)
        ic = self._thumb_icons.get(it.id)
        if ic is not None:
            return ic
        cached = self.thumbs.get(it.id)
        if cached is ...:
            read = self.read_fn
            self.thumbs.request(it.id, lambda it=it: read(it, min(it.size, READ_LIMIT)))
            return self._icon(it, THUMB)
        if cached is None:
            return self._icon(it, THUMB)
        ic = self._thumb_icons[it.id] = QIcon(QPixmap.fromImage(cached))
        return ic

    def _thumb_ready(self, item_id: int, _img):
        row = self.row_of.get(item_id)
        if row is not None:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            iid = self.items[index.row()].id
            if Qt.CheckState(value) == Qt.CheckState.Checked:
                self.checked.add(iid)
            else:
                self.checked.discard(iid)
            self.dataChanged.emit(index, index, [role])
            self.checkedChanged.emit()
            return True
        return False

    def set_checked(self, ids, on: bool):
        if on:
            self.checked |= set(ids)
        else:
            self.checked -= set(ids)
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, 0),
                                  [Qt.ItemDataRole.CheckStateRole])
        self.checkedChanged.emit()

    def checked_items(self) -> List[RecoverableItem]:
        return [i for i in self.items if i.id in self.checked]

    def refresh_rows(self):
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(COLUMNS) - 1))


SIZE_BUCKETS = [("Any size", None), ("Under 1 MB", (0, 2**20)), ("1 – 100 MB", (2**20, 100 * 2**20)),
                ("Over 100 MB", (100 * 2**20, None)), ("Not empty", (1, None))]
DATE_BUCKETS = [("Any date", None), ("Last 7 days", 7), ("Last 30 days", 30), ("Last 12 months", 365)]
CHANCE_FILTERS = [("Any chance", None), ("Excellent or good", {"Excellent", "Good"}),
                  ("Excellent only", {"Excellent"}), ("Poor", {"Poor"}), ("Include unlikely", "all")]


class FileFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.pred = lambda it: True
        self.category = None
        self.folder: Optional[str] = None
        self.chances = None             # None: hide only overwritten files; "all": everything; set: those
        self.size = None
        self.days = None
        self.setSortRole(SORT_ROLE)

    def refresh(self):
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        it: RecoverableItem = self.sourceModel().items[row]
        if self.chances is None:
            if it.hidden_by_default:
                return False
        elif self.chances != "all" and it.chance not in self.chances:
            return False
        if self.folder is not None and not (it.folder == self.folder or it.folder.startswith(self.folder + "/")):
            return False
        if self.category and it.category != self.category:
            return False
        if self.size:
            lo, hi = self.size
            if it.size < lo or (hi is not None and it.size >= hi):
                return False
        if self.days:
            if not it.modified or it.modified < datetime.now() - timedelta(days=self.days):
                return False
        return self.pred(it)


class _PreviewJob(QRunnable):
    def __init__(self, owner: "PreviewPane", gen: int, item, read_fn, limit: int):
        super().__init__()
        self.owner, self.gen, self.item, self.read_fn, self.limit = owner, gen, item, read_fn, limit

    def run(self):
        try:
            data = self.read_fn(self.item, self.limit)
        except Exception as e:
            data = e
        self.owner._loaded.emit(self.gen, self.item, data)


class PreviewPane(QFrame):
    _loaded = pyqtSignal(int, object, object)
    recoverOne = pyqtSignal(object)
    verifyOne = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setMinimumWidth(260)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(label("Preview", "sectionTitle"))
        self.stack = QStackedWidget()
        self.empty = label("Select a file to preview it here.", "muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image = QLabel()
        self.image.setObjectName("card")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(200)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = QFont(theme.mono_family())
        f.setPointSize(9)
        self.text.setFont(f)
        for w in (self.empty, self.image, self.text):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 3)
        self.warning = label("", "badText", wrap=True)
        self.warning.setVisible(False)
        lay.addWidget(self.warning)
        self.info = label("", wrap=True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.info, 1)
        btns = QHBoxLayout()
        self.check_btn = QPushButton("Check file")
        self.check_btn.setToolTip("Read the file and test whether its structure is intact.")
        self.save_btn = QPushButton("Recover this file…")
        btns.addWidget(self.check_btn)
        btns.addStretch(1)
        btns.addWidget(self.save_btn)
        lay.addLayout(btns)
        self.check_btn.clicked.connect(lambda: self.item and self.verifyOne.emit(self.item))
        self.save_btn.clicked.connect(lambda: self.item and self.recoverOne.emit(self.item))
        self.item: Optional[RecoverableItem] = None
        self.read_fn = None
        self.gen = 0
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)
        self._loaded.connect(self._on_loaded, Qt.ConnectionType.QueuedConnection)
        self._pix: Optional[QPixmap] = None
        self.clear()

    def clear(self):
        self.item = None
        self.gen += 1
        self.stack.setCurrentWidget(self.empty)
        self.empty.setText("Select a file to preview it here.")
        self.info.setText("")
        self.warning.setVisible(False)
        self.check_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

    def show_item(self, it: RecoverableItem):
        self.item = it
        self.gen += 1
        self._pix = None
        self.stack.setCurrentWidget(self.empty)
        self.empty.setText("Reading…")
        self.warning.setVisible(False)
        self.check_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.set_info(it)
        if self.read_fn:
            limit = 32 * 1024 * 1024 if it.category == "Images" else 256 * 1024
            self.pool.clear()
            self.pool.start(_PreviewJob(self, self.gen, it, self.read_fn, min(limit, it.size)))

    def _on_loaded(self, gen: int, it: RecoverableItem, data):
        if gen != self.gen:
            return  # the person already moved on to another file
        if isinstance(data, Exception):
            self.empty.setText(f"Couldn't read this file: {data}")
            return
        warn = ""
        if it.condition == Condition.ERASED and it.deleted and it.file_path is None:
            warn = ("This file's data was already erased by the drive (SSD TRIM): it reads back as zeros. "
                    "Use \"Find surviving copies\" to look for a restore point, preview or autosave of it.")
        elif it.size and data and not any(data[:65536]):
            warn = ("The start of this file reads back as zeros. On SSDs the drive erases deleted data (TRIM), "
                    "so this file is probably gone.")
        elif it.condition == Condition.OVERWRITTEN and it.deleted:
            warn = "Another file now uses this space. What you recover will be that file's data."
        self.warning.setText(warn)
        self.warning.setVisible(bool(warn))
        pm = QPixmap()
        if it.category == "Images" and pm.loadFromData(data):
            self._pix = pm
            self._fit()
            self.stack.setCurrentWidget(self.image)
            return
        self.text.setPlainText(_as_text(data) or _hexdump(data[:4096]))
        self.stack.setCurrentWidget(self.text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._pix is not None:
            self._fit()

    def _fit(self):
        target = self.image.size()
        self.image.setPixmap(self._pix.scaled(max(target.width() - 16, 100), max(target.height() - 16, 100),
                                              Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))

    def set_info(self, it: RecoverableItem):
        rows = [("Name", it.name), ("Folder", it.folder or "(top folder)"),
                ("Size", f"{human_size(it.size)} ({it.size:,} bytes)")]
        if it.created:
            rows.append(("Created", it.created.strftime("%Y-%m-%d %H:%M")))
        if it.modified:
            rows.append(("Modified", it.modified.strftime("%Y-%m-%d %H:%M")))
        if it.deleted_at:
            rows.append(("Deleted", it.deleted_at.strftime("%Y-%m-%d %H:%M")))
        chance = it.chance if it.deleted else "Still exists"
        rows.append(("Chance", f"<span style='color:{theme.chance_color(it.chance)};font-weight:600'>{chance}</span>"
                               f" — {_esc(chance_help(it) if it.deleted else 'This file is not deleted.')}"))
        if it.verified:
            rows.append(("Check", {"ok": "Looks intact", "damaged": "Looks damaged",
                                   "unknown": "Can't be checked automatically"}[it.verified]))
        rows.append(("Found by", it.source))
        if it.extents and len(it.extents) > 1:
            rows.append(("Pieces on disk", str(len(it.extents))))
        if it.note and it.condition not in (Condition.ERASED, Condition.PARTLY_ERASED):
            rows.append(("Note", it.note))
        html = "".join(f"<tr><td style='color:{theme.color('muted')};padding:2px 12px 2px 0;vertical-align:top'>{k}</td>"
                       f"<td style='padding:2px 0'>{v if k == 'Chance' else _esc(v)}</td></tr>" for k, v in rows)
        self.info.setText(f"<table>{html}</table>")


def _as_text(data: bytes) -> Optional[str]:
    sample = data[:32768]
    if not sample:
        return "(empty file)"
    for enc in (("utf-16",) if sample[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8", "cp1252")):
        try:
            txt = sample.decode(enc)
        except UnicodeDecodeError:
            continue
        if sum(c.isprintable() or c in "\r\n\t" for c in txt) / max(len(txt), 1) > 0.95:
            return txt
    return None


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hexdump(b: bytes) -> str:
    lines = []
    for i in range(0, len(b), 16):
        row = b[i:i + 16]
        hexs = " ".join(f"{x:02X}" for x in row)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        lines.append(f"{i:06X}  {hexs:<47}  {asc}")
    return "\n".join(lines) or "(empty file)"


class ResultsPage(QWidget):
    recoverRequested = pyqtSignal(list, bool)
    verifyRequested = pyqtSignal(list)
    newScanRequested = pyqtSignal()
    deepScanRequested = pyqtSignal()
    saveRequested = pyqtSignal()
    survivorsRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = label("Found files", "pageTitle")
        self.lead = label("", "pageLead", wrap=True)
        titles.addWidget(self.title)
        titles.addWidget(self.lead)
        head.addLayout(titles, 1)
        self.deep_btn = QPushButton("Search deeper")
        self.deep_btn.setToolTip("Run a deep scan to find files whose records are gone.")
        self.deep_btn.clicked.connect(self.deepScanRequested)
        save = QPushButton("Save scan…")
        save.setToolTip("Save these results so you can come back without scanning again (Ctrl+S).")
        save.clicked.connect(self.saveRequested)
        new = QPushButton("New scan")
        new.clicked.connect(self.newScanRequested)
        self.surv_btn = QPushButton("Find surviving copies")
        self.surv_btn.setToolTip("Look for copies the drive didn't erase: restore points and snapshots, "
                                 "photo thumbnail caches, and Office autosaves.")
        self.surv_btn.clicked.connect(self.survivorsRequested)
        for b in (self.deep_btn, self.surv_btn, save, new):
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        self.banner = QVBoxLayout()
        lay.addLayout(self.banner)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search: name, *.jpg, size:>1mb, modified:2024 …")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip(SEARCH_HELP)
        self.category = QComboBox()
        self.category.addItem("All types", None)
        for c in [*CATEGORIES.keys(), "Other"]:
            self.category.addItem(c, c)
        self.chance = QComboBox()
        for t, v in CHANCE_FILTERS:
            self.chance.addItem(t, v)
        self.chance.setToolTip("Overwritten files are hidden unless you include them.")
        self.size = QComboBox()
        for t, v in SIZE_BUCKETS:
            self.size.addItem(t, v)
        self.date = QComboBox()
        for t, v in DATE_BUCKETS:
            self.date.addItem(t, v)
        bar.addWidget(self.search, 3)
        bar.addWidget(help_button(SEARCH_HELP.replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
                                  .replace("<code>", "").replace("</code>", "").replace("&nbsp;", " ")
                                  .replace("&gt;", ">")))
        for w in (self.category, self.chance, self.size, self.date):
            bar.addWidget(w)
        self.view_list = QToolButton()
        self.view_list.setText("☰ List")
        self.view_grid = QToolButton()
        self.view_grid.setText("▦ Gallery")
        for b in (self.view_list, self.view_grid):
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.view_list.setChecked(True)
        self.view_list.clicked.connect(lambda: self.set_view(False))
        self.view_grid.clicked.connect(lambda: self.set_view(True))
        bar.addWidget(self.view_list)
        bar.addWidget(self.view_grid)
        lay.addLayout(bar)

        self.model = FileModel()
        self.proxy = FileFilter()
        self.proxy.setSourceModel(self.model)

        # sidebar
        self.side = QTabWidget()
        self.side.setMinimumWidth(200)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.types = QListWidget()
        self.types.setObjectName("sideList")
        self.side.addTab(self.types, "Types")
        self.side.addTab(self.tree, "Folders")

        # list
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        h = self.table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        h.setStretchLastSection(False)
        for c, wdt in enumerate((210, 160, 72, 118, 104, 96)):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            h.resizeSection(c, wdt)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        # gallery
        self.grid = QListView()
        self.grid.setModel(self.proxy)
        self.grid.setModelColumn(0)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(QSize(THUMB, THUMB))
        self.grid.setGridSize(QSize(THUMB + 26, THUMB + 46))
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(200)

        self.views = QStackedWidget()
        self.views.addWidget(self.table)
        self.views.addWidget(self.grid)
        self.empty_note = label("", "muted", wrap=True)
        self.empty_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.views.addWidget(self.empty_note)

        self.preview = PreviewPane()
        split = QSplitter()
        split.addWidget(self.side)
        split.addWidget(self.views)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setChildrenCollapsible(False)
        self.split = split
        self._sized = False
        lay.addWidget(split, 1)

        foot = QHBoxLayout()
        check_all = QPushButton("Check all shown")
        check_all.setToolTip("Tick every file that matches the current search and filters.")
        uncheck = QPushButton("Clear checks")
        self.selection = label("No files checked", "muted")
        self.keep_structure = QCheckBox("Keep original folders")
        self.keep_structure.setChecked(True)
        self.verify_btn = QPushButton("Check before recovering")
        self.verify_btn.setToolTip("Read the checked files and test whether they're intact. Nothing is written.")
        self.verify_btn.setEnabled(False)
        self.recover_btn = QPushButton("Recover checked files…")
        self.recover_btn.setObjectName("primary")
        self.recover_btn.setEnabled(False)
        foot.addWidget(check_all)
        foot.addWidget(uncheck)
        foot.addSpacing(10)
        foot.addWidget(self.selection, 1)
        foot.addWidget(self.keep_structure)
        foot.addSpacing(8)
        foot.addWidget(self.verify_btn)
        foot.addWidget(self.recover_btn)
        lay.addLayout(foot)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _: self._debounce.start())
        for cb in (self.category, self.chance, self.size, self.date):
            cb.currentIndexChanged.connect(self._apply_filter)
        self.tree.currentItemChanged.connect(self._on_folder)
        self.types.currentRowChanged.connect(self._on_type)
        check_all.clicked.connect(lambda: self._check_visible(True))
        uncheck.clicked.connect(lambda: self.model.set_checked([i.id for i in self.model.items], False))
        self.model.checkedChanged.connect(self._update_selection)
        self.table.selectionModel().currentRowChanged.connect(self._on_row)
        self.grid.selectionModel().currentChanged.connect(self._on_row)
        self.table.customContextMenuRequested.connect(lambda p: self._menu(self.table, p))
        self.grid.customContextMenuRequested.connect(lambda p: self._menu(self.grid, p))
        self.grid.doubleClicked.connect(self._toggle_index)
        self.recover_btn.clicked.connect(
            lambda: self.recoverRequested.emit(self.model.checked_items(), self.keep_structure.isChecked()))
        self.verify_btn.clicked.connect(lambda: self.verifyRequested.emit(self.model.checked_items()))
        self.preview.recoverOne.connect(lambda it: self.recoverRequested.emit([it], False))
        self.preview.verifyOne.connect(lambda it: self.verifyRequested.emit([it]))
        self.proxy.rowsInserted.connect(self._update_empty)
        self.proxy.modelReset.connect(self._update_empty)
        self.proxy.layoutChanged.connect(self._update_empty)
        for view in (self.table, self.grid):
            act = QAction(view)
            act.setShortcut(QKeySequence(Qt.Key.Key_Space))
            act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            act.triggered.connect(lambda _=False, v=view: self._toggle_selected(v))
            view.addAction(act)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._sized:
            total = self.split.width()
            self.split.setSizes([190, max(total - 190 - 290, 400), 290])
            self._sized = True

    def restyle(self):
        self.model.restyle()
        self.model.refresh_rows()
        if self.preview.item:
            self.preview.set_info(self.preview.item)

    # ------------------------------------------------------------------ data
    def set_items(self, items: List[RecoverableItem], source_name: str, can_deep: bool, read_fn):
        self.model.set_items(items, read_fn)
        self.preview.read_fn = read_fn
        ok = sum(1 for i in items if i.chance in ("Excellent", "Good"))
        hidden = sum(1 for i in items if i.hidden_by_default)
        erased = sum(1 for i in items if i.deleted and i.file_path is None
                     and i.condition in (Condition.ERASED, Condition.PARTLY_ERASED))
        clear_layout(self.banner)
        if erased:
            btn = QPushButton("Find surviving copies")
            btn.setObjectName("primary")
            btn.clicked.connect(self.survivorsRequested)
            self.banner.addWidget(Notice(
                f"The drive already erased {erased:,} of these files",
                "Their names are still listed, but their data reads back as zeros. On SSDs that's TRIM, and no "
                "software can undo it. Copies often survive elsewhere though: restore points and snapshots, the "
                "photo thumbnail cache, and Office autosaves.", kind="warn", action=btn))
        self.title.setText(f"{len(items):,} files found on {source_name}" if items else f"No files found on {source_name}")
        if items:
            lead = f"{ok:,} have an excellent or good chance. Tick the files you want, then recover them to a different drive."
            if hidden:
                lead += f" {hidden:,} overwritten files are hidden."
        else:
            lead = ("Nothing turned up. Try a deep scan, which searches the drive by file contents." if can_deep
                    else "Nothing turned up on this drive.")
        self.lead.setText(lead)
        self.deep_btn.setVisible(can_deep)
        self.table.setColumnHidden(5, len({i.source for i in items}) <= 1)
        self._build_tree(items)
        self._build_types(items)
        self.preview.clear()
        self.set_view(sum(1 for i in items if i.category == "Images") > len(items) * 0.6 and len(items) > 0)
        self._apply_filter()
        if self.proxy.rowCount():
            self.table.setFocus()

    def _visible_items(self) -> List[RecoverableItem]:
        return [i for i in self.model.items if not i.hidden_by_default]

    def _build_tree(self, items: List[RecoverableItem]):
        self.tree.blockSignals(True)
        self.tree.clear()
        vis = self._visible_items()
        counts: Dict[str, int] = {}
        for it in vis:
            parts = it.folder.split("/") if it.folder else []
            for d in range(1, len(parts) + 1):
                key = "/".join(parts[:d])
                counts[key] = counts.get(key, 0) + 1
        root = QTreeWidgetItem([f"All files ({len(vis):,})"])
        root.setData(0, Qt.ItemDataRole.UserRole, None)
        self.tree.addTopLevelItem(root)
        nodes = {"": root}
        for key in sorted(counts, key=lambda k: (k.count("/"), k.lower())):
            parent_key, _, name = key.rpartition("/")
            node = QTreeWidgetItem([f"{name} ({counts[key]:,})"])
            node.setData(0, Qt.ItemDataRole.UserRole, key)
            nodes.get(parent_key, root).addChild(node)
            nodes[key] = node
        for n in nodes.values():
            n.sortChildren(0, Qt.SortOrder.AscendingOrder)
        if len(nodes) < 200:
            self.tree.expandAll()
        else:
            root.setExpanded(True)
        self.tree.setCurrentItem(root)
        self.tree.blockSignals(False)
        self.proxy.folder = None

    def _build_types(self, items: List[RecoverableItem]):
        self.types.blockSignals(True)
        self.types.clear()
        vis = self._visible_items()
        counts: Dict[str, int] = {}
        sizes: Dict[str, int] = {}
        for it in vis:
            counts[it.category] = counts.get(it.category, 0) + 1
            sizes[it.category] = sizes.get(it.category, 0) + it.size
        all_item = QListWidgetItem(f"All files  ·  {len(vis):,}")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.types.addItem(all_item)
        for cat in [*CATEGORIES.keys(), "Other"]:
            if counts.get(cat):
                li = QListWidgetItem(type_icon("", cat, 18), f"{cat}  ·  {counts[cat]:,}")
                li.setToolTip(human_size(sizes[cat]))
                li.setData(Qt.ItemDataRole.UserRole, cat)
                self.types.addItem(li)
        surv = sum(1 for i in vis if i.source in SURVIVOR_SOURCES)
        if surv and surv != len(vis):
            li = QListWidgetItem(f"Surviving copies  ·  {surv:,}")
            li.setToolTip("Restore points, thumbnail previews and autosaves")
            li.setData(Qt.ItemDataRole.UserRole, "__surv__")
            self.types.addItem(li)
        trash = sum(1 for i in vis if i.source == Source.TRASH)
        if trash and trash != len(vis):
            li = QListWidgetItem(f"In Recycle Bin  ·  {trash:,}")
            li.setData(Qt.ItemDataRole.UserRole, "__trash__")
            self.types.addItem(li)
        self.types.setCurrentRow(0)
        self.types.blockSignals(False)

    # ------------------------------------------------------------------ filters
    def _apply_filter(self, *_):
        q = self.search.text().strip()
        pred = compile_query(q)
        if getattr(self, "_trash_only", False):
            pred0 = pred
            pred = lambda it: it.source == Source.TRASH and pred0(it)  # noqa: E731
        if getattr(self, "_surv_only", False):
            pred1 = pred
            pred = lambda it: it.source in SURVIVOR_SOURCES and pred1(it)  # noqa: E731
        self.proxy.pred = pred
        self.proxy.category = self.category.currentData()
        self.proxy.chances = self.chance.currentData()
        self.proxy.size = self.size.currentData()
        self.proxy.days = self.date.currentData()
        self.proxy.refresh()
        self._update_empty()

    def _update_empty(self, *_):
        if not self.model.items:
            self.empty_note.setText("No files to show.")
            self.views.setCurrentWidget(self.empty_note)
        elif self.proxy.rowCount() == 0:
            self.empty_note.setText("No files match your search and filters.\n\nTry fewer words, or set the filters "
                                    "above back to \"All types\" and \"Any …\".")
            self.views.setCurrentWidget(self.empty_note)
        else:
            self.views.setCurrentWidget(self.grid if self.model.gallery else self.table)

    def _on_folder(self, cur, _prev):
        self.proxy.folder = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self.proxy.refresh()
        self._update_empty()

    def _on_type(self, row: int):
        it = self.types.item(row)
        data = it.data(Qt.ItemDataRole.UserRole) if it else None
        self._trash_only = data == "__trash__"
        self._surv_only = data == "__surv__"
        idx = self.category.findData(None if data in (None, "__trash__", "__surv__") else data)
        self.category.blockSignals(True)
        self.category.setCurrentIndex(max(idx, 0))
        self.category.blockSignals(False)
        self._apply_filter()

    def set_view(self, gallery: bool):
        self.model.gallery = gallery
        self.view_list.setChecked(not gallery)
        self.view_grid.setChecked(gallery)
        self.model.refresh_rows()
        self._update_empty()

    # ------------------------------------------------------------------ checking
    def _shown_ids(self) -> List[int]:
        return [self.proxy.data(self.proxy.index(r, 0), ITEM_ROLE).id for r in range(self.proxy.rowCount())]

    def _check_visible(self, on: bool):
        self.model.set_checked(self._shown_ids(), on)

    def _selected_items(self, view) -> List[RecoverableItem]:
        rows = {i.row() for i in view.selectionModel().selectedIndexes()}
        return [self.proxy.data(self.proxy.index(r, 0), ITEM_ROLE) for r in sorted(rows)]

    def _toggle_selected(self, view):
        items = self._selected_items(view)
        if items:
            on = any(i.id not in self.model.checked for i in items)
            self.model.set_checked([i.id for i in items], on)

    def _toggle_index(self, idx):
        it = self.proxy.data(self.proxy.index(idx.row(), 0), ITEM_ROLE)
        self.model.set_checked([it.id], it.id not in self.model.checked)

    def _update_selection(self):
        items = self.model.checked_items()
        if items:
            self.selection.setText(f"{len(items):,} files checked, {human_size(sum(i.size for i in items))}")
        else:
            self.selection.setText("Tick the files you want back.")
        self.recover_btn.setEnabled(bool(items))
        self.verify_btn.setEnabled(bool(items))

    def _menu(self, view, pos):
        items = self._selected_items(view)
        m = QMenu(self)
        if items:
            m.addAction(f"Check {len(items):,} selected", lambda: self.model.set_checked([i.id for i in items], True))
            m.addAction("Uncheck selected", lambda: self.model.set_checked([i.id for i in items], False))
            m.addSeparator()
            m.addAction("Recover selected…", lambda: self.recoverRequested.emit(items, self.keep_structure.isChecked()))
            m.addAction("Check whether selected files are intact", lambda: self.verifyRequested.emit(items))
            m.addSeparator()
            first = items[0]
            if first.folder:
                m.addAction(f"Check everything in “{first.folder.split('/')[-1]}”",
                            lambda: self.model.set_checked([i.id for i in self.model.items
                                                            if i.folder == first.folder
                                                            or i.folder.startswith(first.folder + "/")], True))
                m.addAction("Show this folder", lambda: self._select_folder(first.folder))
            m.addAction("Copy original path", lambda: QGuiApplication.clipboard().setText(first.full_path))
        m.addAction("Check all shown", lambda: self._check_visible(True))
        m.exec(view.viewport().mapToGlobal(pos))

    def _select_folder(self, folder: str):
        it = self.tree.invisibleRootItem()
        stack = [it]
        while stack:
            node = stack.pop()
            if node.data(0, Qt.ItemDataRole.UserRole) == folder:
                self.side.setCurrentWidget(self.tree)
                self.tree.setCurrentItem(node)
                self.tree.scrollToItem(node)
                return
            stack += [node.child(i) for i in range(node.childCount())]

    def _on_row(self, cur, _prev=None):
        if not cur.isValid():
            return
        it = self.proxy.data(self.proxy.index(cur.row(), 0), ITEM_ROLE)
        if it is not None:
            self.preview.show_item(it)

    def refresh_item(self, it: RecoverableItem):
        self.model.refresh_rows()
        if self.preview.item is it:
            self.preview.set_info(it)
