"""Reusable widgets: step rail, cards, drive rows, notices, stat tiles and the live disk map."""
from __future__ import annotations

import os
from typing import List, Optional

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
                             QSizePolicy, QToolButton, QToolTip, QVBoxLayout, QWidget)

from . import theme
from ..core.models import human_size

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def asset(name: str) -> str:
    return os.path.join(ASSETS, name)


def label(text: str, obj: Optional[str] = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    return lb


def badge(text: str, kind: str = "") -> QLabel:
    """kind: "", "warn", "good" or "accent"."""
    lb = QLabel(text)
    lb.setObjectName({"warn": "badgeWarn", "good": "badgeGood", "accent": "badgeAccent"}.get(kind, "badge"))
    return lb


def restyle(w: QWidget):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


def scroll_page() -> tuple:
    """A scrollable page body: returns (outer widget, inner vertical layout)."""
    outer = QWidget()
    outer.setObjectName("page")
    ol = QVBoxLayout(outer)
    ol.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    body = QWidget()
    body.setObjectName("page")
    lay = QVBoxLayout(body)
    lay.setContentsMargins(40, 32, 40, 26)
    lay.setSpacing(14)
    scroll.setWidget(body)
    ol.addWidget(scroll)
    return outer, lay


def help_button(tip: str) -> QToolButton:
    """A small (?) that explains a term on hover or click."""
    b = QToolButton()
    b.setText("?")
    b.setToolTip(tip)
    b.setFixedSize(QSize(22, 22))
    b.setCursor(Qt.CursorShape.WhatsThisCursor)
    b.clicked.connect(lambda: QToolTip.showText(b.mapToGlobal(b.rect().bottomLeft()), tip, b))
    return b


class Notice(QFrame):
    """Inline message box. kind: "warn", "info" or "good"."""

    def __init__(self, title: str, body: str, kind: str = "warn", action: Optional[QPushButton] = None):
        super().__init__()
        self.setObjectName({"warn": "notice", "good": "noticeGood"}.get(kind, "noticeInfo"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(title, "noticeTitle"))
        self.body = label(body, wrap=True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.body)
        lay.addLayout(text, 1)
        if action:
            lay.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)


class StepRail(QFrame):
    """Left rail showing where the person is in the recovery flow."""

    STEPS = ["What happened", "Choose a drive", "Scan", "Review files", "Recover"]

    def __init__(self):
        super().__init__()
        self.setObjectName("rail")
        self.setFixedWidth(220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 24, 16, 18)
        lay.setSpacing(2)
        brand = QHBoxLayout()
        brand.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(QIcon(asset("icon.svg")).pixmap(40, 40))
        mark.setFixedSize(40, 40)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Lifeline", "appName"))
        names.addWidget(label("File recovery", "appTag"))
        brand.addWidget(mark)
        brand.addLayout(names, 1)
        lay.addLayout(brand)
        lay.addSpacing(28)
        self._rows: List[QLabel] = []
        for i, s in enumerate(self.STEPS, 1):
            row = label(f"{i}   {s}", "stepRow")
            row.setMinimumHeight(34)
            self._rows.append(row)
            lay.addWidget(row)
        lay.addStretch(1)
        self.tools = QVBoxLayout()
        self.tools.setSpacing(2)
        lay.addLayout(self.tools)
        lay.addSpacing(10)
        self.footer = label("Read-only. Lifeline never writes to the drive it scans.", "railFoot", wrap=True)
        lay.addWidget(self.footer)
        self.set_step(0)

    def add_tool(self, text: str, slot, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("link")
        b.setToolTip(tip)
        b.clicked.connect(slot)
        self.tools.addWidget(b)
        return b

    def set_step(self, idx: int):
        for i, row in enumerate(self._rows):
            row.setProperty("state", "current" if i == idx else "done" if i < idx else "todo")
            restyle(row)


class ClickableFrame(QFrame):
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)


class ScenarioCard(ClickableFrame):
    """A big friendly choice on the first page: an emoji-free glyph, a title and one line."""

    def __init__(self, glyph: str, title: str, body: str, hint: str = ""):
        super().__init__()
        self.setObjectName("scenarioCard")
        self.setProperty("selected", False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(118)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(16)
        g = label(glyph, "scenarioIcon")
        g.setFixedWidth(40)
        g.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(g)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(label(title, "cardTitle", wrap=True))
        text.addWidget(label(body, wrap=True))
        if hint:
            text.addWidget(label(hint, "muted", wrap=True))
        text.addStretch(1)
        lay.addLayout(text, 1)
        self.setAccessibleName(title)
        self.setAccessibleDescription(body)

    def set_selected(self, on: bool):
        self.setProperty("selected", on)
        restyle(self)


class DriveRow(ClickableFrame):
    def __init__(self, vol, recommended: bool = False):
        super().__init__()
        self.vol = vol
        self.setObjectName("driveRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(16)
        letter = label(vol.letter + ":" if vol.letter else ("⛁" if vol.is_disk else "▤"), "driveLetter")
        letter.setFixedWidth(44)
        letter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(letter)

        mid = QVBoxLayout()
        mid.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label(vol.title, "driveName"))
        if not vol.letter:
            top.addWidget(label(vol.name, "muted"))
        if vol.filesystem:
            top.addWidget(badge(vol.filesystem, "" if vol.fs_supported or vol.is_disk else "warn"))
        if vol.kind != vol.title:
            top.addWidget(badge(vol.kind))
        if vol.media == "SSD" and vol.trim:
            top.addWidget(badge("SSD · TRIM on", "warn"))
        if vol.is_system:
            top.addWidget(badge("System drive"))
        if recommended:
            top.addWidget(badge("Suggested", "accent"))
        top.addStretch(1)
        mid.addLayout(top)
        if vol.free is not None and vol.total:
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int((vol.total - vol.free) / vol.total * 1000))
            bar.setFixedHeight(6)
            mid.addWidget(bar)
            sub = f"{human_size(vol.free)} free of {human_size(vol.total)}"
        else:
            sub = human_size(vol.total)
        if vol.mountpoint and not vol.letter:
            sub += f"  ·  {vol.mountpoint}"
        if not vol.fs_supported and not vol.is_disk and vol.filesystem:
            sub += "  ·  Deep scan only (finds files by content, without names)"
        if not vol.readable:
            sub += "  ·  Needs administrator rights"
        mid.addWidget(label(sub, "muted", wrap=True))
        lay.addLayout(mid, 1)
        go = label("›", "driveLetter")
        lay.addWidget(go)
        self.setAccessibleName(f"{vol.title} {vol.name}")


class OptionCard(ClickableFrame):
    def __init__(self, title: str, body: str, meta: str, tag: str = ""):
        super().__init__()
        self.setObjectName("optionCard")
        self.setProperty("selected", False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(label(title, "cardTitle"))
        if tag:
            top.addWidget(badge(tag, "accent"))
        top.addStretch(1)
        lay.addLayout(top)
        lay.addWidget(label(body, wrap=True))
        self.meta = label(meta, "muted", wrap=True)
        lay.addWidget(self.meta)
        lay.addStretch(1)
        self.setAccessibleName(title)

    def set_selected(self, on: bool):
        self.setProperty("selected", on)
        restyle(self)


class StatTile(QFrame):
    def __init__(self, title: str, value: str = "0", obj: str = "bigNumber"):
        super().__init__()
        self.setObjectName("statTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(0)
        self.value = label(value, obj)
        lay.addWidget(self.value)
        lay.addWidget(label(title, "muted"))

    def set(self, value: str):
        self.value.setText(value)


class Collapsible(QWidget):
    """A "Show advanced options" section, closed by default so beginners aren't overwhelmed."""

    def __init__(self, title: str, body: QWidget, open_: bool = False):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.title = title
        self.btn = QPushButton()
        self.btn.setObjectName("link")
        self.btn.clicked.connect(self.toggle)
        lay.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.body = body
        lay.addWidget(body)
        self.set_open(open_)

    def toggle(self):
        self.set_open(not self.body.isVisible())

    def set_open(self, on: bool):
        self.body.setVisible(on)
        self.btn.setText(("▾  Hide " if on else "▸  Show ") + self.title)


class DiskMap(QWidget):
    """A grid standing in for the whole drive. Cells fill as the scan sweeps past;
    cells where files were found get a solid mark. Parallel scans fill out of order."""

    COLS = 80

    def __init__(self, cells: int = 1440):
        super().__init__()
        self.cells = cells
        self.state = bytearray(cells)  # 0 untouched, 1 scanned, 2 file found
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Scan progress map")

    def resizeEvent(self, e):
        self.setFixedHeight(self.heightForWidth(self.width()))
        super().resizeEvent(e)

    def reset(self):
        self.state = bytearray(self.cells)
        self.update()

    def mark(self, fraction: float, found: bool = False):
        """Everything up to `fraction` has been searched (engines report in disk order)."""
        idx = max(0, min(int(fraction * self.cells) - 1, self.cells - 1))
        for i in range(idx + 1):
            if self.state[i] == 0:
                self.state[i] = 1
        if found:
            self.state[idx] = 2
        self.update()

    def fill_to(self, fraction: float):
        idx = min(int(fraction * self.cells), self.cells)
        for i in range(idx):
            if self.state[i] == 0:
                self.state[i] = 1
        self.update()

    def complete(self):
        for i in range(self.cells):
            if self.state[i] == 0:
                self.state[i] = 1
        self.update()

    def heightForWidth(self, w: int) -> int:
        rows = -(-self.cells // self.COLS)
        return max(60, int(rows * (w / self.COLS)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rows = -(-self.cells // self.COLS)
        size = min(self.width() / self.COLS, self.height() / rows)
        gap = max(1.5, size * 0.18)
        colors = {0: theme.qcolor("map_empty"), 1: theme.qcolor("map_done"), 2: theme.qcolor("accent")}
        p.setPen(QPen(Qt.PenStyle.NoPen))
        for i, s in enumerate(self.state):
            r, c = divmod(i, self.COLS)
            rect = QRectF(c * size + gap / 2, r * size + gap / 2, size - gap, size - gap)
            p.setBrush(colors[s])
            p.drawRoundedRect(rect, 1.5, 1.5)
        p.end()


def dot_icon(color: str, size: int = 12) -> QIcon:
    from PyQt6.QtGui import QPixmap
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.end()
    return QIcon(pm)
