"""Palettes and stylesheet, light and dark.

Cool mineral neutrals keep the screen calm for someone who has just lost
files; signal blue marks the next action; green, amber and red are reserved
for recovery chances so they always mean the same thing.

Widgets never hard-code colours: they use object names styled here, or call
color() at paint time, so switching theme restyles everything live.
"""
from __future__ import annotations

from typing import Dict

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette

LIGHT: Dict[str, str] = {
    "bg": "#EDF0EE", "surface": "#FFFFFF", "surface2": "#F6F8F7", "rail": "#E2E7E4", "ink": "#1D2A2E",
    "muted": "#5A686D", "line": "#CFD7D3", "accent": "#1F5FAD", "accent_hover": "#184F92",
    "accent_soft": "#DCE7F5", "accent_ink": "#FFFFFF", "accent_disabled": "#9DB4D3",
    "good": "#2E7A4C", "good_soft": "#E1F1E7", "warn": "#B9821B", "warn_soft": "#FBF2DC", "warn_line": "#E3C784",
    "warn_ink": "#7A5410", "bad": "#B0392C", "bad_soft": "#F8E3E0", "info_line": "#B9CDEA",
    "map_empty": "#CFD7D3", "map_done": "#9DB9DD", "disabled": "#9AA6AA", "disabled_bg": "#F3F5F4",
}

DARK: Dict[str, str] = {
    "bg": "#161B1D", "surface": "#1F2629", "surface2": "#232B2E", "rail": "#12171A", "ink": "#E3E9E7",
    "muted": "#93A1A5", "line": "#334046", "accent": "#5B9BE6", "accent_hover": "#77AEEE",
    "accent_soft": "#22364F", "accent_ink": "#0B1220", "accent_disabled": "#2E4560",
    "good": "#5BBF85", "good_soft": "#1B3326", "warn": "#E0AE4A", "warn_soft": "#3A2F17", "warn_line": "#6B5626",
    "warn_ink": "#F1D193", "bad": "#E8776A", "bad_soft": "#3D2220", "info_line": "#34506F",
    "map_empty": "#334046", "map_done": "#3B5F8A", "disabled": "#5F6C70", "disabled_bg": "#1B2124",
}

_current: Dict[str, str] = dict(LIGHT)
_mode = "light"


def color(name: str) -> str:
    return _current[name]


def qcolor(name: str) -> QColor:
    return QColor(_current[name])


def is_dark() -> bool:
    return _mode == "dark"


def chance_color(chance: str) -> str:
    return {"Excellent": color("good"), "Good": color("good"), "Poor": color("warn"),
            "Unlikely": color("bad")}.get(chance, color("muted"))


def condition_color(cond: str) -> str:
    return {"Good": color("good"), "Partly overwritten": color("warn"), "Overwritten": color("bad")}.get(
        cond, color("muted"))


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt < 6.5
        pal = QGuiApplication.palette()
        return pal.color(QPalette.ColorRole.Window).lightness() < 128


def app_font() -> QFont:
    families = set(QFontDatabase.families())
    for name in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", ".AppleSystemUIFont", "Inter",
                 "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans"):
        if name in families:
            return QFont(name, 10)
    return QFont()


def mono_family() -> str:
    families = set(QFontDatabase.families())
    for name in ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "JetBrains Mono", "DejaVu Sans Mono",
                 "Liberation Mono", "Noto Sans Mono"):
        if name in families:
            return name
    return "monospace"


def apply(app, mode: str = "system"):
    """mode: "light", "dark" or "system"."""
    global _mode, _current
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    _mode = "dark" if dark else "light"
    _current = dict(DARK if dark else LIGHT)
    app.setStyle("Fusion")  # identical look on Windows, macOS and Linux
    app.setFont(app_font())
    c = _current
    pal = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
                      (QPalette.ColorRole.AlternateBase, "surface2"), (QPalette.ColorRole.Text, "ink"),
                      (QPalette.ColorRole.WindowText, "ink"), (QPalette.ColorRole.Button, "surface"),
                      (QPalette.ColorRole.ButtonText, "ink"), (QPalette.ColorRole.Highlight, "accent_soft"),
                      (QPalette.ColorRole.HighlightedText, "ink"), (QPalette.ColorRole.ToolTipBase, "surface"),
                      (QPalette.ColorRole.ToolTipText, "ink"), (QPalette.ColorRole.PlaceholderText, "muted"),
                      (QPalette.ColorRole.Link, "accent")):
        pal.setColor(role, QColor(c[key]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(c["disabled"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(c["disabled"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(c["disabled"]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(c))


def stylesheet(c: Dict[str, str]) -> str:
    return f"""
QWidget {{ color: {c['ink']}; }}
QMainWindow, #page, QScrollArea, QScrollArea > QWidget > QWidget#page {{ background: {c['bg']}; }}
QToolTip {{ background: {c['surface']}; color: {c['ink']}; border: 1px solid {c['line']}; padding: 6px; }}

#rail {{ background: {c['rail']}; border-right: 1px solid {c['line']}; }}
#appName {{ font-size: 17pt; font-weight: 600; }}
#appTag, #muted, #railFoot {{ color: {c['muted']}; }}
#stepRow {{ color: {c['muted']}; border-left: 3px solid transparent; padding: 6px 0 6px 10px; }}
#stepRow[state="current"] {{ color: {c['accent']}; font-weight: 600; border-left: 3px solid {c['accent']}; }}
#stepRow[state="done"] {{ color: {c['ink']}; border-left: 3px solid {c['line']}; }}

#pageTitle {{ font-size: 20pt; font-weight: 600; }}
#pageLead {{ color: {c['muted']}; font-size: 10.5pt; }}
#sectionTitle {{ font-size: 11pt; font-weight: 600; }}
#bigNumber {{ font-size: 26pt; font-weight: 600; }}
#good {{ color: {c['good']}; }}
#warnText {{ color: {c['warn']}; }}
#badText {{ color: {c['bad']}; font-weight: 600; }}

QPushButton {{
    background: {c['surface']}; border: 1px solid {c['line']}; border-radius: 6px; padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton:focus {{ border: 2px solid {c['accent']}; }}
QPushButton:disabled {{ color: {c['disabled']}; background: {c['disabled_bg']}; }}
QPushButton:checked {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
QPushButton#primary {{
    background: {c['accent']}; color: {c['accent_ink']}; border: 1px solid {c['accent']};
    font-weight: 600; padding: 9px 20px;
}}
QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
QPushButton#primary:disabled {{ background: {c['accent_disabled']}; border-color: {c['accent_disabled']}; color: {c['bg']}; }}
QPushButton#danger {{ color: {c['bad']}; }}
QPushButton#link {{ border: none; background: transparent; color: {c['accent']}; padding: 4px 0; text-align: left; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QPushButton#iconButton {{ padding: 6px 9px; min-width: 18px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px; }}
QToolButton:hover {{ border-color: {c['line']}; background: {c['surface']}; }}
QToolButton:checked {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}

#card, #driveRow, #optionCard, #scenarioCard, #statTile {{
    background: {c['surface']}; border: 1px solid {c['line']}; border-radius: 10px;
}}
#driveRow:hover, #optionCard:hover, #scenarioCard:hover {{ border-color: {c['accent']}; }}
#driveRow:focus, #optionCard:focus, #scenarioCard:focus {{ border: 2px solid {c['accent']}; }}
#optionCard[selected="true"], #scenarioCard[selected="true"] {{ border: 2px solid {c['accent']}; background: {c['accent_soft']}; }}
#driveRow[disabled="true"] {{ background: {c['surface2']}; }}
#driveLetter {{ font-size: 18pt; font-weight: 600; color: {c['accent']}; }}
#driveName, #cardTitle {{ font-size: 11.5pt; font-weight: 600; }}
#scenarioIcon {{ font-size: 22pt; }}
#card QLabel, #driveRow QLabel, #optionCard QLabel, #scenarioCard QLabel, #statTile QLabel, #notice QLabel,
#noticeInfo QLabel, #noticeGood QLabel {{ background: transparent; }}

#badge {{ border: 1px solid {c['line']}; border-radius: 9px; padding: 1px 8px; color: {c['muted']}; background: {c['bg']}; }}
#badgeWarn {{ border: 1px solid {c['warn_line']}; border-radius: 9px; padding: 1px 8px; color: {c['warn_ink']}; background: {c['warn_soft']}; }}
#badgeGood {{ border: 1px solid {c['good']}; border-radius: 9px; padding: 1px 8px; color: {c['good']}; background: {c['good_soft']}; }}
#badgeAccent {{ border: 1px solid {c['accent']}; border-radius: 9px; padding: 1px 8px; color: {c['accent']}; background: {c['accent_soft']}; }}

#notice {{ background: {c['warn_soft']}; border: 1px solid {c['warn_line']}; border-radius: 8px; }}
#noticeInfo {{ background: {c['accent_soft']}; border: 1px solid {c['info_line']}; border-radius: 8px; }}
#noticeGood {{ background: {c['good_soft']}; border: 1px solid {c['good']}; border-radius: 8px; }}
#noticeTitle {{ font-weight: 600; }}

QProgressBar {{
    border: none; background: {c['line']}; border-radius: 4px; max-height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}

QLineEdit, QComboBox {{
    background: {c['surface']}; border: 1px solid {c['line']}; border-radius: 6px; padding: 6px 8px;
    selection-background-color: {c['accent']}; selection-color: {c['accent_ink']};
}}
QLineEdit:focus, QComboBox:focus {{ border: 2px solid {c['accent']}; }}
QComboBox QAbstractItemView {{ background: {c['surface']}; border: 1px solid {c['line']}; selection-background-color: {c['accent_soft']}; }}

QTableView, QTreeView, QTreeWidget, QListView, QListWidget, QPlainTextEdit, QTextBrowser {{
    background: {c['surface']}; border: 1px solid {c['line']}; border-radius: 8px;
    selection-background-color: {c['accent_soft']}; selection-color: {c['ink']};
    alternate-background-color: {c['surface2']};
}}
QTableView {{ gridline-color: transparent; }}
QTableView::item, QTreeView::item, QTreeWidget::item {{ padding: 4px 6px; }}
QListView#sideList::item {{ padding: 7px 8px; border-radius: 6px; }}
QListView#sideList::item:selected {{ background: {c['accent_soft']}; color: {c['ink']}; }}
QHeaderView::section {{
    background: {c['surface']}; border: none; border-bottom: 1px solid {c['line']};
    padding: 7px 6px; color: {c['muted']}; font-weight: 600;
}}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; padding: 6px 12px; color: {c['muted']}; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {c['accent']}; border-bottom: 2px solid {c['accent']}; font-weight: 600; }}
QSplitter::handle {{ background: transparent; width: 8px; }}
QCheckBox {{ spacing: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['line']}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['line']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QMenu {{ background: {c['surface']}; border: 1px solid {c['line']}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['accent_soft']}; }}
QDialog {{ background: {c['bg']}; }}
"""
