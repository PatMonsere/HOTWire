"""The SystemCore's own NT server (port 6810): ControlData decoding, the reader, and how its
state joins robot code's (it may only ADD the field lock)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("HOTWIRE_HOME", tempfile.mkdtemp(prefix="hotwire_sys_"))

from hotwire.systemserver import SystemReader, control_word_from_proto, decode_control  # noqa: E402
from tests.test_nt_fake_robot import BACKENDS, _ports, client, start_fake, wait_for  # noqa: E402

# Real ProtobufControlData messages from the Driver Station's log, 2026-11-25 (FIRST_DS_20261126_001646)
REAL = {
    "1a0e08ffff01183f220600010001000028a68002": (0x8026, "utility", False),
    "1a0e08ffff01183f2206000100010000212bfb1895ffffff0228a48002": (0x8024, "teleoperated", False),
    "1a0e08ffff01183f2206000100010000212bfb1895ffffff0228a58002": (0x8025, "teleoperated", True),
    "1a0e08ffff01183f220600010001000021000000000000000128a28002": (0x8022, "autonomous", False),
    "1a0e08ffff01183f22060001000100001a001a001a001a001a00212b1a4c210000000128a38002": (0x8023, "autonomous", True),
}


def test_decode_real_control_data():
    for hexmsg, (word, mode, enabled) in REAL.items():
        w = control_word_from_proto(bytes.fromhex(hexmsg))
        assert w == word
        d = decode_control(w)
        assert d["mode"] == mode and d["enabled"] == enabled and d["ds_attached"]
        assert not d["fms_attached"] and not d["estop"]
    assert decode_control(0x802C)["estop"]  # seen in the DS logs after e-stops
    assert decode_control(0x8034)["fms_attached"]  # bit 4: not seen with a real FMS yet
    assert control_word_from_proto(b"") == 0  # proto3: a missing field is 0
    assert control_word_from_proto(b"\x28") is None  # cut off
    assert control_word_from_proto("x") is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_reader_and_field_mode(backend):
    port = next(_ports)
    p = start_fake(port, "--system")
    r = SystemReader(client=client(backend, port), port=port)
    r.start(["127.0.0.1"])
    try:
        assert wait_for(lambda: r.read().battery is not None and r.read().control is not None)
        s = r.read()
        assert s.control["mode"] == "utility" and not s.control["enabled"]
        assert abs(s.battery - 11.55) < 1e-6 and s.has_user_code is True and s.emmc == "good"
        assert "robot code ✓" in s.summary() and "4.6 GB free" in s.summary() and not s.problem()
        r.set_field_mode(True)  # on the field: only ControlData stays
        s = r.read()
        assert s.control is not None and s.battery is None and r.field_mode
        r.set_field_mode(False)
        assert wait_for(lambda: r.read().battery is not None)
    finally:
        r.stop()
        p.kill()


def _ctx(robot_port, system_port):
    from hotwire.settings import HostEntry, Settings
    from hotwire.ui.context import AppContext

    s = Settings()
    s.save_folder = tempfile.mkdtemp()
    s.nt_backend = "builtin"
    s.nt_port = robot_port
    s.system_port = system_port
    s.device("SystemCore").hosts = [HostEntry("127.0.0.1")]
    return AppContext(s)


def test_no_robot_code_state_comes_from_systemcore():
    rport, sport = next(_ports), next(_ports)  # nothing listens on rport: robot code not running
    p = start_fake(sport, "--system", "--no-code")
    ctx = _ctx(rport, sport)
    try:
        assert wait_for(lambda: ctx.update_state().known)
        st = ctx.update_state()
        assert not st.robot_code and not st.enabled and not st.fms_attached and "no robot code" in st.label
        assert ctx.gate(manual=False, transfer=True).allowed  # auto log pull works without robot code
        assert abs(ctx.battery_voltage() - 11.55) < 1e-6
        assert ctx.system_status.problem()  # no robot code is worth a look
    finally:
        ctx.stop_nt()
        p.kill()


def test_systemcore_fms_adds_the_field_lock():
    rport, sport = next(_ports), next(_ports)
    pr = start_fake(rport)  # robot code says: pits
    ps = start_fake(sport, "--system", "--fms")  # SystemCore says: FMS
    ctx = _ctx(rport, sport)
    try:
        assert wait_for(lambda: ctx.update_state().known and ctx.system_status.control is not None)
        time.sleep(0.5)
        st = ctx.update_state()
        assert st.fms_attached and st.robot_code
        assert not ctx.gate(manual=True, transfer=False).allowed
        assert ctx.system.field_mode  # health subscriptions dropped on the field
    finally:
        ctx.stop_nt()
        pr.kill()
        ps.kill()
