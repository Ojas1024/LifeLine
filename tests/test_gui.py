"""Offscreen smoke test of the whole GUI flow: open image, smart scan, results, recover."""
import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
sys.path.insert(0, os.path.dirname(__file__))
import imagegen  # noqa: E402
import samplegen  # noqa: E402


@pytest.mark.skipif(not imagegen.have("mkfs.fat", "mcopy"), reason="needs dosfstools + mtools")
def test_gui_flow(tmp_path, monkeypatch):
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    os.environ["MTOOLS_SKIP_CHECK"] = "1"
    img = str(tmp_path / "c.img")
    imagegen.make_fat(img, 32, 65536, "CARD")
    imagegen.mcopy(img, samplegen.jpeg(), "Holiday photo.jpg", str(tmp_path))
    imagegen.run("mdel", "-i", img, "::Holiday photo.jpg")

    app = QApplication.instance() or QApplication([])
    from lifeline.ui import theme
    from lifeline.ui.main_window import MainWindow
    theme.apply(app, "dark")
    w = MainWindow()
    w.show()

    def pump(sec, until=None):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            if until and until():
                return
            time.sleep(0.02)

    w.choose_scenario("deleted")
    assert w.stack.currentIndex() == w.DRIVES
    assert w._open(img, "card")
    assert w.stack.currentIndex() == w.SETUP
    w.setup.select("smart")
    w.setup._start()
    pump(60, lambda: w.stack.currentIndex() == w.RESULTS)
    assert w.stack.currentIndex() == w.RESULTS
    names = [i.name for i in w.session.items]
    assert "Holiday photo.jpg" in names
    w.results.search.setText("type:photos")
    pump(0.5)
    assert w.results.proxy.rowCount() >= 1
    w.results.set_view(True)
    pump(1.0)
    w.results._check_visible(True)
    dest = str(tmp_path / "out")
    os.makedirs(dest)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: dest))
    w.results.recover_btn.click()
    pump(30, lambda: w.recover_page.open_btn.isVisible())
    assert w.recover_page.open_btn.isVisible()
    assert (tmp_path / "out" / "Holiday photo.jpg").read_bytes() == samplegen.jpeg()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    w.close()
