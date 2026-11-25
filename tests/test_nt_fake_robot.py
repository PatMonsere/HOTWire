"""End-to-end: NT client + robot state + recorder + analysis against tools/fake_robot.py.

Runs for both NetworkTables backends (built-in and pyntcore). Proves they work
against our own fake server, NOT against the real robot: check that at the shop.
"""

from __future__ import annotations

import itertools
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
_ports = itertools.count(15840)

try:
    from hotwire.nt_pyntcore import PyntcoreClient, available

    HAVE_PYNTCORE = available()[0]
except Exception:  # Smart App Control on / not installed
    HAVE_PYNTCORE = False

BACKENDS = ["builtin"] + (["pyntcore"] if HAVE_PYNTCORE else [])


def client(backend, port):
    return NT4Client(port=port) if backend == "builtin" else PyntcoreClient(port=port)


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


@pytest.fixture(params=BACKENDS)
def pits(request):
    port = next(_ports)
    p = start_fake(port)
    nt = client(request.param, port)
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
    assert pits.connected_host == "127.0.0.1"


@pytest.mark.parametrize("backend", BACKENDS)
def test_fms_blocks(backend):
    port = next(_ports)
    p = start_fake(port, "--fms")
    nt = client(backend, port)
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
    time.sleep(7.5)  # covers the intake extend check (fake cycle: extend at 2 s, done by ~3.7 s)
    rec.stop()
    data = rec.snapshot()
    n = len(data["/HOT/Intake/Extend/IntakeExtendSupply Current"])
    assert n > 100, f"send-all should deliver every value, got {n}"
    # robot (server) timestamps line up with server_time_us()
    last_t = data["/HOT/Intake/Extend/IntakeExtendSupply Current"][-1][0]
    assert abs(last_t - pits.server_time_us() / 1e6) < 2.0
    res = an.analyze(cfg, data, final=True)
    assert res[0].status in ("pass", "warn"), an.report_text(res)


def test_topic_list(pits):
    uid = pits.subscribe(["/"], prefix=True, topics_only=True)
    assert wait_for(lambda: any(t.name == "/HOTVision/LEFT/connected" for t in pits.topics()))
    pits.unsubscribe(uid)


def test_restart_keeps_subscriptions(pits):
    """Competition lock stops and restarts the client: subscriptions must come back."""
    reader = RobotStateReader(pits)
    assert wait_for(lambda: reader.read().known)
    pits.stop()
    assert not pits.connected
    pits.start(["127.0.0.1"])
    assert wait_for(lambda: reader.read().known), "state subscription lost after restart"
