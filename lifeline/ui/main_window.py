"""Main window: ties the guided pages to scan sessions and background tasks."""
from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QMainWindow, QMessageBox,
                             QProgressDialog, QStackedWidget, QWidget)

from . import theme
from .dialogs import HelpDialog, ImageDialog, PartitionsDialog, SettingsDialog, settings
from .pages import DrivePage, RecoverPage, ScanningPage, SetupPage, StartPage
from .results import ResultsPage
from .tasks import Task
from .widgets import StepRail, asset
from ..core import devices
from ..core.disk import DiskError, create_image, is_device_path
from ..core.models import RecoverableItem, human_size
from ..core.recovery import read_head, recover, same_drive
from ..core.session import ScanSession

SESSION_FILTER = "Lifeline scan (*.lifeline)"
IMAGE_FILTER = "Disk images (*.img *.dd *.raw *.bin *.iso *.001 *.ima *.vhd);;All files (*)"


class MainWindow(QMainWindow):
    START, DRIVES, SETUP, SCANNING, RESULTS, RECOVER = range(6)
    RAIL = {START: 0, DRIVES: 1, SETUP: 2, SCANNING: 2, RESULTS: 3, RECOVER: 4}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lifeline File Recovery")
        self.resize(1440, 880)
        self.setMinimumSize(1060, 660)
        self.setAcceptDrops(True)
        self.session: Optional[ScanSession] = None
        self.volume: Optional[devices.VolumeInfo] = None
        self.scenario = "deleted"
        self.task: Optional[Task] = None
        self.side_tasks: List[Task] = []
        self.last_mode = "smart"
        self.last_dest = ""

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.rail = StepRail()
        h.addWidget(self.rail)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.rail.add_tool("Open a saved scan…", self.open_session_file)
        self.rail.add_tool("Settings", self.show_settings)
        self.rail.add_tool("Help  (F1)", self.show_help)

        self.start = StartPage()
        self.drives = DrivePage()
        self.setup = SetupPage()
        self.scanning = ScanningPage()
        self.results = ResultsPage()
        self.recover_page = RecoverPage()
        for w in (self.start, self.drives, self.setup, self.scanning, self.results, self.recover_page):
            self.stack.addWidget(w)

        self.start.chosen.connect(self.choose_scenario)
        self.start.openImage.connect(self.open_image)
        self.start.openSession.connect(self.open_session_file)
        self.drives.chosen.connect(self.choose_volume)
        self.drives.openImage.connect(self.open_image)
        self.drives.makeImage.connect(self.make_image)
        self.drives.restartAdmin.connect(self.restart_admin)
        self.drives.back.connect(lambda: self.go(self.START))
        self.setup.start.connect(self.start_scan)
        self.setup.back.connect(self._setup_back)
        self.setup.partitionChanged.connect(self._switch_partition)
        self.scanning.stop.connect(self.stop_scan)
        self.scanning.pauseToggled.connect(self._pause)
        self.results.recoverRequested.connect(self.recover_files)
        self.results.verifyRequested.connect(self.verify_files)
        self.results.newScanRequested.connect(self.go_home)
        self.results.deepScanRequested.connect(self._deep_from_results)
        self.results.saveRequested.connect(self.save_session)
        self.recover_page.openFolder.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_dest)))
        self.recover_page.backToResults.connect(lambda: self.go(self.RESULTS))
        self.recover_page.startOver.connect(self.go_home)
        self.recover_page.cancel.connect(self._cancel_recover)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_image)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_session)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=lambda: self.results.recover_btn.isEnabled()
                  and self.stack.currentIndex() == self.RESULTS and self.results.recover_btn.click())
        QShortcut(QKeySequence("F1"), self, activated=self.show_help)
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(self._system_theme_changed)
        except AttributeError:
            pass
        self.go(self.START)

    # ------------------------------------------------------------ navigation
    def go(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.rail.set_step(self.RAIL[idx])

    def go_home(self):
        if self.task and self.task.isRunning():
            return
        self._close_session()
        self.go(self.START)

    def _setup_back(self):
        if self.volume is None:
            self.go(self.START)
        else:
            self.drives.refresh()
            self.go(self.DRIVES)

    def _close_session(self):
        if self.session:
            self.session.close()
            self.session = None

    def _focus_search(self):
        if self.stack.currentIndex() == self.RESULTS:
            self.results.search.setFocus()
            self.results.search.selectAll()

    def restart_admin(self):
        if devices.relaunch_as_admin():
            QApplication.quit()
        else:
            QMessageBox.warning(self, "Couldn't restart",
                                "The operating system didn't allow the restart.\n\n" + devices.elevation_hint())

    def _workers(self) -> Optional[int]:
        n = int(settings().value("workers", 0))
        return n or None

    # ------------------------------------------------------------ sources
    def choose_scenario(self, key: str):
        self.scenario = key
        if key == "trash":
            self.scan_trash()
            return
        self.drives.set_scenario(key)
        self.go(self.DRIVES)

    def choose_volume(self, vol: devices.VolumeInfo):
        self.volume = vol
        name = vol.title + (f" ({vol.letter}:)" if vol.letter else f" ({vol.name})")
        self._open(vol.path, name, vol.media)

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open disk image", "", IMAGE_FILTER)
        if path:
            self.volume = None
            self._open(path, os.path.basename(path))

    def _open(self, path: str, name: str, media: str = "Unknown") -> bool:
        if self.task and self.task.isRunning():
            return False
        self._close_session()
        try:
            self.session = ScanSession(path, name, media=media)
        except DiskError as e:
            box = QMessageBox(QMessageBox.Icon.Warning, "Can't open the drive", str(e), parent=self)
            if not devices.is_admin() and is_device_path(path):
                restart = box.addButton("Restart with administrator rights", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is restart:
                    self.restart_admin()
            else:
                box.exec()
            return False
        self.setup.prepare(self.session, self.volume, self.scenario)
        self.go(self.SETUP)
        return True

    def _switch_partition(self, p):
        if not self.session:
            return
        self.session.use_partition(p)
        self.setup.prepare(self.session, self.volume, self.scenario)

    def make_image(self):
        dlg = ImageDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vol = dlg.src.currentData()
        if vol is None:
            return
        dest = dlg.dest_path
        prog = QProgressDialog("Copying drive…", "Stop", 0, 1000, self)
        prog.setWindowTitle("Making an image")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setMinimumWidth(480)
        task = Task(lambda p, c, f, b: create_image(vol.path, dest, p, c), self)
        task.progress.connect(lambda fr, m: (prog.setValue(int(fr * 1000)), prog.setLabelText(m)))
        prog.canceled.connect(task.cancel)

        def finished(res):
            prog.close()
            if not res["complete"]:
                QMessageBox.information(self, "Image paused", f"Copied {human_size(res['bytes'])} so far. Choose the "
                                        "same file again later to continue where it stopped.")
                return
            msg = f"Image saved to {dest}."
            if res["unreadable_blocks"]:
                msg += f"\n{res['unreadable_blocks']:,} unreadable 4 KB blocks were filled with zeros."
            if QMessageBox.question(self, "Image ready", msg + "\n\nScan the image now?") == \
                    QMessageBox.StandardButton.Yes:
                self.volume = None
                self._open(dest, os.path.basename(dest))

        task.done.connect(finished)
        task.failed.connect(lambda m: (prog.close(), QMessageBox.critical(self, "Imaging stopped", m)))
        self._keep(task)
        task.start()

    # ------------------------------------------------------------ scans
    def scan_trash(self):
        self._close_session()
        self.volume = None
        self.session = ScanSession(None, "the Recycle Bin")
        s = self.session
        self.last_mode = "trash"
        self._run_scan("Looking in the Recycle Bin", lambda p, c, f, b: s.scan_trash(p, c), track_map=False,
                       show_map=False, total=0)

    def start_scan(self, opts: dict):
        s = self.session
        if not s:
            return
        mode = opts["mode"]
        self.last_mode = mode
        workers = opts.get("workers") or self._workers()
        if mode == "partitions":
            self._run_scan(f"Searching {s.display_name} for lost partitions",
                           lambda p, c, f, b: s.search_partitions(p, c), track_map=True, total=s.disk.length,
                           on_done=self._partitions_found)
            return
        if mode == "quick":
            fn = lambda p, c, f, b: s.quick_scan(p, c, f, include_existing=opts["include_existing"],  # noqa: E731
                                                 workers=workers)
            title = f"Quick scan of {s.display_name}"
        elif mode == "deep":
            fn = lambda p, c, f, b: s.deep_scan(opts["keys"], opts["free_only"], p, c, f, b, workers)  # noqa: E731
            title = f"Deep scan of {s.display_name}"
        else:
            def fn(p, c, f, b):
                out = []
                if s.fs:
                    out += s.quick_scan(lambda fr, m: p(fr * 0.25, "Step 1 of 2: " + m), c, f,
                                        include_existing=opts["include_existing"], workers=workers)
                if c():
                    return out
                lo = 0.25 if s.fs else 0.0
                out += s.deep_scan(opts["keys"], opts["free_only"] and s.fs is not None,
                                   lambda fr, m: p(lo + fr * (1 - lo), ("Step 2 of 2: " if s.fs else "") + m),
                                   c, f, b, workers)
                return out
            title = f"Smart scan of {s.display_name}"
        self._run_scan(title, fn, track_map=mode == "quick", total=s.size)

    def _run_scan(self, title: str, fn, track_map: bool, total: int, show_map: bool = True, on_done=None):
        self.scanning.begin(title, total, show_map=show_map, can_pause=True)
        self.task = Task(fn, self)
        self.task.progress.connect(lambda fr, m: self.scanning.on_progress(fr, m, track_map))
        self.task.found.connect(self.scanning.on_found)
        self.task.block.connect(lambda fr, hit: self.scanning.map.mark(fr, hit))
        self.task.done.connect(on_done or self._scan_done)
        self.task.failed.connect(self._scan_failed)
        self.go(self.SCANNING)
        self.task.start()

    def _pause(self, on: bool):
        if self.task:
            self.task.pause() if on else self.task.resume()

    def stop_scan(self):
        if self.task:
            self.scanning.stopping()
            self.task.cancel()

    def _scan_done(self, _found):
        self.scanning.map.complete()
        QTimer.singleShot(300, self._show_results)

    def _show_results(self):
        s = self.session
        if not s:
            return
        reader = s.reader
        can_deep = reader is not None and self.last_mode in ("quick", "loaded")
        self.results.set_items(s.items, s.display_name, can_deep=can_deep,
                               read_fn=lambda it, n: read_head(reader, it, n))
        self.go(self.RESULTS)

    def _scan_failed(self, msg: str):
        QMessageBox.warning(self, "Scan stopped", msg)
        self.go(self.SETUP if self.session and self.session.reader else self.START)

    def _partitions_found(self, found):
        s = self.session
        self.scanning.map.complete()
        parts = s.partitions or found
        if not parts:
            QMessageBox.information(self, "No partitions found",
                                    "No partitions turned up. A deep scan of the whole disk can still find files "
                                    "by their contents.")
            s.use_whole_disk()
            self.setup.prepare(s, self.volume, "formatted")
            self.setup.select("deep")
            self.go(self.SETUP)
            return
        dlg = PartitionsDialog(self, parts, sum(1 for p in found if p.origin == "Found"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.setup.prepare(s, self.volume, self.scenario)
            self.go(self.SETUP)
            return
        if dlg.whole_disk:
            s.use_whole_disk()
            self.setup.prepare(s, self.volume, "formatted")
            self.setup.select("deep")
        else:
            p = dlg.selected()
            s.use_partition(p)
            self.setup.prepare(s, self.volume, "deleted")
        self.go(self.SETUP)

    def _deep_from_results(self):
        if self.session and self.session.reader:
            self.setup.prepare(self.session, self.volume, self.scenario)
            self.setup.select("deep")
            self.go(self.SETUP)

    # ------------------------------------------------------------ verify
    def verify_files(self, items: List[RecoverableItem]):
        s = self.session
        if not s or not items:
            return
        prog = QProgressDialog("Checking files…", "Stop", 0, 1000, self)
        prog.setWindowTitle("Checking files")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(300)
        prog.setMinimumWidth(420)
        task = Task(lambda p, c, f, b: s.verify(items, p, c), self)
        task.progress.connect(lambda fr, m: (prog.setValue(int(fr * 1000)), prog.setLabelText(m)))
        prog.canceled.connect(task.cancel)

        def done(counts):
            prog.close()
            for it in items:
                self.results.refresh_item(it)
            if len(items) == 1:
                it = items[0]
                text = {"ok": "This file looks intact.",
                        "damaged": f"This file looks damaged. {it.note}",
                        "unknown": "This type of file can't be checked automatically. Recover it and open it."}[
                    it.verified or "unknown"]
                QMessageBox.information(self, it.name, text)
            else:
                QMessageBox.information(self, "Check finished",
                                        f"{counts.get('ok', 0):,} look intact, {counts.get('damaged', 0):,} look "
                                        f"damaged, {counts.get('unknown', 0):,} can't be checked automatically.\n\n"
                                        "Damaged files are marked Poor; you can still recover them.")
        task.done.connect(done)
        task.failed.connect(lambda m: (prog.close(), QMessageBox.warning(self, "Check stopped", m)))
        self._keep(task)
        task.start()

    # ------------------------------------------------------------ recovery
    def recover_files(self, items: List[RecoverableItem], keep_structure: bool):
        s = self.session
        if not s or not items:
            return
        start_dir = settings().value("last_dest", "") or os.path.expanduser("~")
        title = "Recover to… (choose a folder on a different drive)"
        dest = QFileDialog.getExistingDirectory(self, title, start_dir)
        if not dest:
            return
        if s.source_path and same_drive(s.source_path, dest):
            QMessageBox.warning(self, "Choose a different drive",
                                f"{dest} is on the drive you're recovering from. Saving there can overwrite the files "
                                "you're trying to get back.\n\nPick a folder on another drive or a USB stick.")
            return
        total = sum(i.size for i in items)
        try:
            free = shutil.disk_usage(dest).free
            if free < total:
                QMessageBox.warning(self, "Not enough space",
                                    f"These files need {human_size(total)}, but {dest} has {human_size(free)} free.")
                return
        except OSError:
            pass
        settings().setValue("last_dest", dest)
        self.last_dest = dest
        cfg = settings()
        workers = int(cfg.value("recover_workers", 4))
        if s.media == "HDD":
            workers = min(workers, 2)
        verify = cfg.value("verify", True, type=bool)
        self.recover_page.begin(len(items), total, dest)
        self.go(self.RECOVER)
        task = Task(lambda p, c, f, b: recover(s.reader, s.source_path, items, dest, keep_structure, p, c,
                                               workers=workers, verify=verify), self)
        task.progress.connect(self.recover_page.on_progress)
        task.done.connect(self._recovered)
        task.failed.connect(lambda m: (QMessageBox.critical(self, "Recovery stopped", m), self.go(self.RESULTS)))
        self.recover_task = task
        self._keep(task)
        task.start()

    def _cancel_recover(self):
        t = getattr(self, "recover_task", None)
        if t:
            t.cancel()
            self.recover_page.cancel_btn.setEnabled(False)

    def _recovered(self, res):
        self.recover_page.finish(res)
        self.results.model.refresh_rows()

    # ------------------------------------------------------------ sessions
    def save_session(self):
        s = self.session
        if not s or not s.items:
            return
        default = os.path.join(settings().value("last_dest", "") or os.path.expanduser("~"),
                               f"{s.display_name}.lifeline".replace(":", "").replace("/", "_"))
        path, _ = QFileDialog.getSaveFileName(self, "Save scan", default, SESSION_FILTER)
        if not path:
            return
        if not path.endswith(".lifeline"):
            path += ".lifeline"
        if s.source_path and same_drive(s.source_path, path):
            QMessageBox.warning(self, "Choose a different drive", "Save the scan on another drive than the one "
                                "you're recovering from.")
            return
        try:
            s.save(path)
        except OSError as e:
            QMessageBox.warning(self, "Couldn't save", str(e))
            return
        self.statusBar().showMessage(f"Scan saved to {path}", 6000)

    def open_session_file(self, path: Optional[str] = None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open saved scan", "", SESSION_FILTER)
        if not path:
            return
        self._close_session()
        try:
            self.session = ScanSession.load(path)
        except DiskError as e:
            if "isn't available" in str(e):
                QMessageBox.information(self, "Find the drive or image", f"{e}\n\nChoose the image file, or connect "
                                        "the drive and try again.")
                alt, _ = QFileDialog.getOpenFileName(self, "Where is the image now?", "", IMAGE_FILTER)
                if not alt:
                    return
                try:
                    self.session = ScanSession.load(path, source_override=alt)
                except DiskError as e2:
                    QMessageBox.warning(self, "Can't open scan", str(e2))
                    return
            else:
                QMessageBox.warning(self, "Can't open scan", str(e))
                return
        self.volume = None
        self.last_mode = "loaded"
        self._show_results()

    # ------------------------------------------------------------ settings / help / theme
    def show_settings(self):
        if SettingsDialog(self).exec() == QDialog.DialogCode.Accepted:
            self.apply_theme()

    def show_help(self):
        HelpDialog(self).exec()

    def apply_theme(self):
        theme.apply(QApplication.instance(), settings().value("theme", "system"))
        self.scanning.restyle()
        self.results.restyle()
        for w in self.findChildren(QWidget):
            w.update()

    def _system_theme_changed(self, *_):
        if settings().value("theme", "system") == "system":
            self.apply_theme()

    # ------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        path = urls[0]
        if path.endswith(".lifeline"):
            self.open_session_file(path)
        elif os.path.isfile(path):
            self.volume = None
            self._open(path, os.path.basename(path))

    # ------------------------------------------------------------ housekeeping
    def _keep(self, task: Task):
        self.side_tasks = [t for t in self.side_tasks if t.isRunning()]
        self.side_tasks.append(task)

    def closeEvent(self, e):
        running = [t for t in [self.task, *self.side_tasks] if t and t.isRunning()]
        if running:
            if QMessageBox.question(self, "Stop and quit?", "A scan or recovery is still running. Stop it and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        for t in running:
            t.cancel()
            t.wait(5000)
        self._close_session()
        super().closeEvent(e)


def run():
    if sys.platform == "win32":  # show our icon in the taskbar instead of Python's
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lifeline.FileRecovery")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Lifeline File Recovery")
    app.setOrganizationName("Lifeline")
    app.setWindowIcon(QIcon(asset("icon.svg")))
    theme.apply(app, settings().value("theme", "system"))
    w = MainWindow()
    w.show()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args and os.path.exists(args[-1]):
        if args[-1].endswith(".lifeline"):
            w.open_session_file(args[-1])
        else:
            w._open(args[-1], os.path.basename(args[-1]))
    sys.exit(app.exec())
