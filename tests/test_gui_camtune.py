"""Camera tuning end to end: Cameras tab vs the fake robot's fake HOTVision web API.

Tunes two cameras, checks the result and the before picture, Keep / Undo, the
crash recovery (a pending tuning from an earlier session is put back) and that
quitting puts back settings that weren't kept.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = Path(__file__).parents[1]
NT_PORT, WEB_PORT = 15850, 15851


def test_camera_tuning_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTWIRE_HOME", tempfile.mkdtemp(prefix="hotwire_camtune_"))
    from PySide6.QtWidgets import QApplication, QMessageBox

    from hotwire import camtune as ct
    from hotwire.app import MainWindow
    from hotwire.settings import HostEntry, Settings
    from hotwire.streams import http_get_json
    from hotwire.ui.context import AppContext

    sys.path.insert(0, str(ROOT / "tools"))
    from fake_robot import FAKE_BEST

    # faster steps for the test (the fake camera changes instantly)
    monkeypatch.setattr(ct, "SETTLE_S", 0.15)
    monkeypatch.setattr(ct, "MEASURE_S", 0.45)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), QMessageBox.StandardButton.Yes)[1])
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(("warning", a[2])))

    errors = []
    sys.excepthook = lambda *a: errors.append(a)
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "fake_robot.py"), "--port", str(NT_PORT), "--web-port", str(WEB_PORT)]
    )
    try:
        time.sleep(1.0)
        app = QApplication.instance() or QApplication([])
        s = Settings()
        s.save_folder = str(tmp_path / "save")
        s.nt_port = NT_PORT
        s.device("SystemCore").hosts = [HostEntry("127.0.0.1")]
        s.device("SystemCore").auto = False
        jet = s.device("Jetson")
        jet.hosts = [HostEntry("127.0.0.1")]
        jet.web_port = WEB_PORT
        s.save()
        ctx = AppContext(s)
        w = MainWindow(ctx)
        w.show()
        cams = w.cams_tab

        def spin(sec, until=None):
            t = time.time()
            while time.time() - t < sec:
                app.processEvents()
                if until is not None and until():
                    return True
                time.sleep(0.02)
            return until() if until else True

        def cam_settings(cam):
            return ct.tuned_settings(http_get_json(jet, s, "/api/cameras")[cam]["settings"])

        start = cam_settings("BACK_RIGHT")
        assert spin(5, lambda: ctx.state.known), ctx.state
        w.show_tab(cams)
        assert spin(5, lambda: len(cams.tiles) == 4), list(cams.tiles)
        # layout: tiles fill the height, the panel sits on the right
        assert cams.compare.isVisible()

        # ---- tune two cameras at once
        cams.start_tuning(["BACK_RIGHT", "LEFT"])
        assert spin(5, lambda: bool(cams.runs)), cams.tune_status.text()
        assert set(cams.pending.cameras("Jetson")) == {"BACK_RIGHT", "LEFT"}  # "before" saved first
        assert spin(60, lambda: not cams.runs), cams.tune_status.text()
        assert not errors, errors
        rev = cams.review["BACK_RIGHT"]
        assert rev["after_m"].tags > rev["before_m"].tags + 1.0, (rev["before_m"], rev["after_m"])
        tuned = cam_settings("BACK_RIGHT")
        # the fake tag counts are noisy (and the SSH ranges are estimated here), so check direction:
        # exposure (the big one) lands near the best, the others don't move away from it
        assert abs(tuned["exposure_time_absolute"] - FAKE_BEST["exposure_time_absolute"]) <= 25, tuned
        for k in ("gamma", "contrast"):
            assert abs(tuned[k] - FAKE_BEST[k]) <= abs(start[k] - FAKE_BEST[k]), (k, start, tuned)
        # before picture for "Show before"
        cams._select("BACK_RIGHT")
        assert cams.tiles["BACK_RIGHT"].before_image is not None
        cams.b_before.setChecked(True)
        assert cams.tiles["BACK_RIGHT"].show_before
        assert "Keep or Undo" in cams.tune_status.text()

        # ---- Keep one, Undo the other
        cams.keep(["BACK_RIGHT"])
        assert "BACK_RIGHT" not in cams.pending.cameras("Jetson")
        assert cam_settings("BACK_RIGHT") == tuned
        left_start = start  # all fake cameras start the same
        cams.undo(["LEFT"])
        assert spin(5, lambda: not cams.pending.cameras("Jetson"))
        assert cam_settings("LEFT") == left_start

        # ---- crash recovery: a pending tuning from an "earlier session" is offered back
        cams.pending.add("Jetson", "FRONT", left_start)
        from hotwire.streams import http_post_json

        http_post_json(jet, s, "/api/cameras/FRONT/settings", FAKE_BEST)  # what the crashed run left behind
        cams._asked_recover = False
        assert spin(5, lambda: not cams.pending.cameras("Jetson")), cams.pending.cameras("Jetson")
        assert any("wasn't" in str(q) or "closed before" in str(q) for q in asked), asked
        assert cam_settings("FRONT") == left_start

        # ---- quitting puts back settings that weren't kept
        cams._select("BACK_LEFT")
        cams.spins["exposure_time_absolute"].setValue(40)
        cams.spins["gamma"].setValue(200)
        cams.apply_settings()
        assert spin(5, lambda: cams.review.get("BACK_LEFT", {}).get("after"))  # Apply finished
        assert "BACK_LEFT" in cams.pending.cameras("Jetson")
        assert cam_settings("BACK_LEFT")["exposure_time_absolute"] == 40.0
        w.close()
        assert cam_settings("BACK_LEFT") == left_start
        assert not cams.pending.cameras("Jetson")
        assert not errors, errors
    finally:
        p.kill()
