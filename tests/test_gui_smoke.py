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
        assert w.battery.text().endswith(" V"), w.battery.text()  # battery readout from NT
        for i in range(w.tabs.count()):
            w.tabs.setCurrentIndex(i)
            spin(0.6)
        w.show_tab(w.cams_tab)
        spin(1.5)
        assert set(w.cams_tab.tiles) == {"LEFT", "FRONT", "BACK_LEFT", "BACK_RIGHT"}
        pipes = w.cams_tab.tiles["LEFT"].pipe
        assert [pipes.itemText(i) for i in range(pipes.count())] == ["apriltag"]  # not "status"
        # Jetson tab: heartbeat and events from NT
        w.show_tab(w.jetson_tab)
        spin(3.0)
        assert w.jetson_tab.hb_vals["alive"].text() == "alive", w.jetson_tab.hb_vals["alive"].text()
        assert w.jetson_tab.hb_vals["vin"].text() == "12.20 V"
        assert w.jetson_tab.events.count() >= 1
        assert "fake: test event" in w.jetson_tab.events.item(0).text()
        # Signals tab: live plot from NT
        st = w.signals_tab
        w.show_tab(st)
        spin(1.2)
        st.area.add_signals(["/HOT/Robot/BatteryVoltage", "/HOT/Drive/TotalCurrent"])
        spin(1.5)
        assert st.buffer.arrays("/HOT/Drive/TotalCurrent")[0].size > 10  # send-all samples arrived
        a, b = st.area.x_range()
        assert abs(b - st._live_now()) < 0.5  # following "now"
        assert st.list_panel._items, "signal tree is empty"
        assert st.unit("/HOT/Robot/BatteryVoltage") == "volt"  # from the topic's "unit" property
        assert st.unit("/HOT/Drive/TotalCurrent") == ""  # no property: unitless, not guessed
        # Logs tab select buttons (no files listed: must not crash)
        w.logs_tab._select(lambda f: True)
        # the window can shrink below the tabs' size (they scroll instead of being cut off)
        assert w.minimumSizeHint().height() < 600, w.minimumSizeHint()
        # pit test: record a few seconds
        w.show_tab(w.pit_tab)
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
        w.close()  # closing the window quits the app (no hiding in the tray)
        assert w.quitting and not ctx.nt.connected
        assert not errors, errors
    finally:
        p.kill()
