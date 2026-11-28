"""Job: callbacks arrive even when the caller keeps no reference to the Job."""

from __future__ import annotations

import gc
import os
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"


def test_fire_and_forget_job_callbacks_arrive():
    from PySide6.QtWidgets import QApplication

    from hotwire.ui.common import Job

    app = QApplication.instance() or QApplication([])
    got = []
    for i in range(20):  # no reference kept: before the fix some of these were lost
        Job(lambda job, i=i: i).start(lambda res: got.append(res))
    Job(lambda job: 1 / 0).start(None, lambda err: got.append("failed"))
    gc.collect()
    t = time.time()
    while time.time() - t < 2 and len(got) < 21:
        app.processEvents()
        time.sleep(0.01)
    assert sorted(x for x in got if x != "failed") == list(range(20))
    assert "failed" in got
    assert not Job._alive  # released once delivered
