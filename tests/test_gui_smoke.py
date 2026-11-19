"""GUI smoke test: build the main window offscreen, connect to the fake robot,
visit every tab, record a short pit test and quit. Catches wiring errors."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOTWIRE_HOME"] = tempfile.mkdtemp(prefix="hotwire_gui_")

ROOT = Path(__file__).parents[1]


def test_gui_smoke(tmp_path):
    from PySide6.QtWidgets import QApplication

    from hotwire.app import MainWindow
    from hotwire.settings import HostEntry, Settings
    from hotwire.ui.context import AppContext

    errors = []
    sys.excepthook = lambda *a: errors.append(a)

    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_robot.py"), "--port", "15820"])
    try:
        time.sleep(0.8)
        app = QApplication.instance() or QApplication([])
        s = Settings()
        s.save_folder = str(tmp_path / "save")
        s.nt_port = 15820
        s.device("SystemCore").hosts = [HostEntry("127.0.0.1")]
        s.device("SystemCore").auto = False  # no SSH server here
        s.device("Jetson").hosts = [HostEntry("127.0.0.1")]
        s.pit_config = str(ROOT / "examples" / "pit_checks.yaml")
        s.save()
        ctx = AppContext(s)
        w = MainWindow(ctx)
        w.show()

        def spin(sec):
            t = time.time()
            while time.time() - t < sec:
                app.processEvents()
                time.sleep(0.02)

        spin(2.5)
        assert ctx.state.known and not ctx.state.fms_attached, ctx.state
        assert "OK" in w.badge.text()
        for i in range(w.tabs.count()):
            w.tabs.setCurrentIndex(i)
            spin(0.6)
        w.tabs.setCurrentWidget(w.cams_tab)
        spin(1.5)
        assert set(w.cams_tab.tiles) == {"LEFT", "FRONT", "BACK_LEFT", "BACK_RIGHT"}
        # pit test: record a few seconds
        w.tabs.setCurrentWidget(w.pit_tab)
        w.pit_tab.start_recording()
        spin(3.0)
        w.pit_tab.stop_recording()
        assert w.pit_tab.results, "no results"
        assert list((tmp_path / "save" / "PitTests").glob("*/report.txt"))
        # competition lock closes NT
        ctx.set_lock(True)
        spin(1.5)
        assert not ctx.nt.connected
        assert "LOCK" in w.badge.text()
        ctx.set_lock(False)
        w.quit()
        assert not errors, errors
    finally:
        p.kill()
