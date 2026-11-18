"""Background thumbnail decoding for the gallery view.

Reading and decoding happen on a small thread pool (QImage is safe off the GUI
thread); results come back through a queued signal. Only rows the view
actually paints request thumbnails, so a 100,000-photo result set stays fast.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Optional

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

THUMB = 144
READ_LIMIT = 24 * 1024 * 1024


def decode_thumb(data: bytes, size: int = THUMB) -> Optional[QImage]:
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    reader.setAutoTransform(True)       # respect EXIF rotation
    src = reader.size()
    if src.isValid() and src.width() > 0 and src.height() > 0:
        reader.setScaledSize(src.scaled(QSize(size, size), Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        return None
    if img.width() > size or img.height() > size:
        img = img.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return img


class _Job(QRunnable):
    def __init__(self, loader: "ThumbLoader", item_id: int, read: Callable[[], bytes]):
        super().__init__()
        self.loader, self.item_id, self.read = loader, item_id, read

    def run(self):
        img = None
        try:
            if self.loader.generation_ok(self.item_id):
                img = decode_thumb(self.read())
        except Exception:
            img = None
        self.loader._done.emit(self.item_id, img)


class ThumbLoader(QObject):
    ready = pyqtSignal(int, object)       # item id, QImage or None (not decodable)
    _done = pyqtSignal(int, object)

    def __init__(self, threads: int = 4, cache: int = 3000):
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(threads)
        self.cache: "OrderedDict[int, object]" = OrderedDict()
        self.pending = set()
        self.max_cache = cache
        self._wanted = set()
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    def get(self, item_id: int):
        """Cached QImage, None if it failed, or ... (Ellipsis) if not loaded yet."""
        if item_id in self.cache:
            self.cache.move_to_end(item_id)
            return self.cache[item_id]
        return ...

    def request(self, item_id: int, read: Callable[[], bytes]):
        if item_id in self.cache or item_id in self.pending:
            return
        self.pending.add(item_id)
        self._wanted.add(item_id)
        self.pool.start(_Job(self, item_id, read))

    def generation_ok(self, item_id: int) -> bool:
        return item_id in self._wanted

    def reset(self):
        self.pool.clear()
        self.cache.clear()
        self.pending.clear()
        self._wanted.clear()

    def _on_done(self, item_id: int, img):
        self.pending.discard(item_id)
        if item_id not in self._wanted:
            return
        self.cache[item_id] = img
        while len(self.cache) > self.max_cache:
            self.cache.popitem(last=False)
        self.ready.emit(item_id, img)
