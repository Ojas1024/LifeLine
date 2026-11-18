"""Run long operations off the UI thread, with progress, pause and cancel."""
from __future__ import annotations

import threading
import time
import traceback

from PyQt6.QtCore import QThread, pyqtSignal


class Task(QThread):
    progress = pyqtSignal(float, str)
    found = pyqtSignal(int)
    block = pyqtSignal(float, bool)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        """fn(progress, cancelled, on_found, on_block) -> result. Runs in a worker thread."""
        super().__init__(parent)
        self._fn = fn
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self._last_emit = 0.0
        self._last_found = 0.0
        self._pending_found = None

    def cancel(self):
        self._cancel.set()
        self._running.set()  # wake a paused task so it can stop

    def pause(self):
        self._running.clear()

    def resume(self):
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def cancelled(self) -> bool:
        # Engines poll this often, which makes it the natural place to hold a pause.
        if not self._running.is_set():
            self._running.wait()
        return self._cancel.is_set()

    def _progress(self, frac: float, msg: str):
        now = time.monotonic()
        if now - self._last_emit > 0.05 or frac >= 1.0:  # ~20 updates/s keeps the UI smooth
            self._last_emit = now
            self.progress.emit(float(frac), msg)
        if not self._running.is_set():
            self._running.wait()

    def _found(self, n: int):
        now = time.monotonic()
        if now - self._last_found > 0.1:
            self._last_found = now
            self.found.emit(int(n))
        else:
            self._pending_found = n

    def run(self):
        try:
            result = self._fn(self._progress, self.cancelled, self._found, self.block.emit)
            if self._pending_found is not None:
                self.found.emit(int(self._pending_found))
            self.done.emit(result)
        except Exception as e:  # surfaced to the user as a readable message
            traceback.print_exc()
            self.failed.emit(str(e) or e.__class__.__name__)
