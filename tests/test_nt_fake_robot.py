"""End-to-end: NT4 client + robot state + recorder + analysis against tools/fake_robot.py.

This proves HOTWire's NT4 client works against our own fake server. It does
NOT prove compatibility with the real robot; check that at the shop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("HOTWIRE_HOME", str(Path(__file__).parent / "_home"))

from hotwire.nt4 import NT4Client  # noqa: E402
from hotwire.pitcheck import analyze as an  # noqa: E402
from hotwire.pitcheck import config as pc  # noqa: E402
from hotwire.pitcheck.recorder import Recorder  # noqa: E402
from hotwire.robotstate import RobotStateReader, gate  # noqa: E402

ROOT = Path(__file__).parents[1]


def start_fake(port, *flags):
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_robot.py"), "--port", str(port), *flags])
    time.sleep(0.8)
    return p


def wait_for(cond, timeout=8.0):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pits():
    p = start_fake(15810)
    nt = NT4Client(port=15810)
    nt.start(["127.0.0.1"])
    yield nt
    nt.stop()
    p.kill()


def test_state_and_gate_in_pits(pits):
    reader = RobotStateReader(pits)
    assert wait_for(lambda: reader.read().known)
    st = reader.read()
    assert st.ds_attached and not st.fms_attached and not st.enabled
    assert gate(st, False, manual=False).allowed


def test_fms_blocks():
    p = start_fake(15811, "--fms")
    nt = NT4Client(port=15811)
    nt.start(["127.0.0.1"])
    try:
        reader = RobotStateReader(nt)
        assert wait_for(lambda: reader.read().known)
        st = reader.read()
        assert st.fms_attached and st.event == "TEST" and st.match == "Q12"
        assert not gate(st, False, manual=True).allowed
        assert not gate(st, False, manual=True, transfer=False).allowed
    finally:
        nt.stop()
        p.kill()


def test_record_pit_test_and_analyze(pits):
    cfg = pc.load(ROOT / "examples" / "pit_checks.yaml")
    rec = Recorder(pits, cfg.all_topics())
    assert wait_for(lambda: pits.connected)
    rec.start()
    time.sleep(7.5)  # covers the intake check (fake cycle: intake at 2-6 s)
    rec.stop()
    data = rec.snapshot()
    n = len(data["/HOT/Intake/Extend/IntakeExtendSupply Current"])
    assert n > 100, f"send-all should deliver every value, got {n}"
    res = an.analyze(cfg, data, final=True)
    intake = res[0]
    assert intake.status in ("pass", "warn"), an.report_text(res)


def test_topic_list(pits):
    uid = pits.subscribe(["/"], prefix=True, topics_only=True)
    assert wait_for(lambda: any(t.name == "/HOTVision/LEFT/connected" for t in pits.topics()))
    pits.unsubscribe(uid)
