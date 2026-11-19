"""Tests for the parts that don't need hardware: log sync safety, analysis, config, wpilog, gate."""

from __future__ import annotations

import hashlib
import os
import struct
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("HOTWIRE_HOME", str(Path(__file__).parent / "_home"))

from hotwire import logsync  # noqa: E402
from hotwire.pitcheck import analyze as an  # noqa: E402
from hotwire.pitcheck import config as pc  # noqa: E402
from hotwire.pitcheck.wpilog import read_wpilog  # noqa: E402
from hotwire.robotstate import RobotState, decode_control_word, gate  # noqa: E402


# ------------------------------------------------------------------ fake robot filesystem
class LocalFS:
    """RemoteFS backed by a local folder (posix-style paths under root)."""

    def __init__(self, root: Path, can_hash=True, corrupt=False, clock_offset=0.0):
        self.root, self.can_hash, self.corrupt, self.clock_offset = root, can_hash, corrupt, clock_offset
        self.removed: list[str] = []

    def _p(self, path):
        return self.root / path.lstrip("/")

    def walk(self, root):
        out = []
        for p in self._p(root).rglob("*"):
            if p.is_file():
                rel = "/" + p.relative_to(self.root).as_posix()
                out.append((rel, p.stat().st_size, p.stat().st_mtime))
        return out

    def now(self):
        return time.time() + self.clock_offset

    def sha256(self, path):
        if not self.can_hash:
            return None
        h = hashlib.sha256(self._p(path).read_bytes()).hexdigest()
        return ("0" * 64) if self.corrupt else h

    def open_read(self, path):
        return open(self._p(path), "rb")

    def remove(self, path):
        self.removed.append(path)
        self._p(path).unlink()

    def rmdir_if_empty(self, path):
        p = self._p(path)
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()

    def disk(self, path):
        return (1, 2)


def make_robot(tmp: Path) -> Path:
    logs = tmp / "robot" / "home" / "systemcore" / "logs"
    (logs / "2026-11-18_10-00-00").mkdir(parents=True)
    (logs / "FRC_20261118_100000_MIGA_Q12.wpilog").write_bytes(os.urandom(300_000))
    (logs / "FRC_20261118_090000.wpilog").write_bytes(b"x" * 1000)
    (logs / "2026-11-18_10-00-00" / "rio.hoot").write_bytes(b"h" * 5000)
    (logs / "notes.txt").write_text("ignore me")
    old = time.time() - 3600
    for p in logs.rglob("*"):
        os.utime(p, (old, old))
    return tmp / "robot"


LOGDIR = "/home/systemcore/logs"


def test_list_skips_other_types_and_marks_in_use(tmp_path):
    root = make_robot(tmp_path)
    live = root / "home/systemcore/logs/FRC_20261118_110000.wpilog"
    live.write_bytes(b"live")  # just modified -> in use
    files = logsync.list_logs(LocalFS(root), LOGDIR, [".wpilog", ".hoot"], 60)
    names = {f.name: f for f in files}
    assert "notes.txt" not in names
    assert names["FRC_20261118_110000.wpilog"].in_use
    assert not names["FRC_20261118_090000.wpilog"].in_use


def test_in_use_uses_robot_clock(tmp_path):
    root = make_robot(tmp_path)
    # robot clock 2 hours behind: an hour-old file looks like it's "from the future" -> in use (safe side)
    files = logsync.list_logs(LocalFS(root, clock_offset=-7200), LOGDIR, [".wpilog"], 60)
    assert all(f.in_use for f in files)


def test_destination_rules(tmp_path):
    rf = logsync.RemoteFile("/l/FRC_20261118_100000_MIGA_Q12.wpilog", "FRC_20261118_100000_MIGA_Q12.wpilog", 1, 0)
    assert logsync.destination("S", "SystemCore", rf) == Path("S/SystemCore/MIGA/Q12/FRC_20261118_100000_MIGA_Q12.wpilog")
    rf2 = logsync.RemoteFile("/l/2026-11-18_10-00-00/rio.hoot", "2026-11-18_10-00-00/rio.hoot", 1, 0)
    assert logsync.destination("S", "SC", rf2) == Path("S/SC/2026-11-18/2026-11-18_10-00-00/rio.hoot")
    rf3 = logsync.RemoteFile("/l/x.hoot", "x.hoot", 1, 0)
    assert logsync.destination("S", "SC", rf3, today="2026-11-19") == Path("S/SC/2026-11-19/x.hoot")
    # 2027 SystemCore names (seen on the robot 2026-11-19)
    w = logsync.RemoteFile("/l/WPILIB_20261118_234315.wpilog", "WPILIB_20261118_234315.wpilog", 1, 0)
    assert logsync.destination("S", "SC", w) == Path("S/SC/2026-11-18/WPILIB_20261118_234315.wpilog")
    wm = logsync.RemoteFile("/l/WPILIB_20270315_141022_MIGA_Q12.wpilog", "WPILIB_20270315_141022_MIGA_Q12.wpilog", 1, 0)
    assert logsync.destination("S", "SC", wm) == Path("S/SC/MIGA/Q12/WPILIB_20270315_141022_MIGA_Q12.wpilog")
    tbd = logsync.RemoteFile("/l/WPILIB_TBD_16250975ee69675a.wpilog", "WPILIB_TBD_16250975ee69675a.wpilog", 1, 0)
    assert logsync.destination("S", "SC", tbd, today="2026-11-19") == Path("S/SC/2026-11-19/WPILIB_TBD_16250975ee69675a.wpilog")
    hoot = logsync.RemoteFile("/l/2026-11-18_19-22-56/2026-11-18_19-22-57.hoot", "2026-11-18_19-22-56/2026-11-18_19-22-57.hoot", 1, 0)
    assert logsync.destination("S", "SC", hoot) == Path("S/SC/2026-11-18/2026-11-18_19-22-56/2026-11-18_19-22-57.hoot")


def test_is_match_log():
    def rf(rel):
        return logsync.RemoteFile("/l/" + rel, rel, 1, 0)

    # real names seen on the SystemCore 2026-11-19: practice logs
    for rel in (
        "WPILIB_20261111_224934.wpilog",
        "WPILIB_TBD_16250975ee69675a.wpilog",
        "2026-11-11_19-07-24/2026-11-11_19-07-26.hoot",
        "2026-11-18_19-22-56/2026-11-18_19-22-57.hoot",
    ):
        assert not logsync.is_match_log(rf(rel)), rel
    for rel in (
        "WPILIB_20270315_141022_MIGA_Q12.wpilog",
        "FRC_20240317_201534_MIGA_E3.wpilog",
        "MIGA_Q12_2027-03-15_14-10-22/rio.hoot",
        "2027-03-15_14-10-22_MIGA_Q12/rio.hoot",
    ):
        assert logsync.is_match_log(rf(rel)), rel


def test_short_download_rejected(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root)
    f = logsync.list_logs(fs, LOGDIR, [".wpilog"], 60)[0]
    f.size += 10  # robot says bigger than what we receive (dropped transfer)
    with pytest.raises(IOError):
        logsync.download(fs, f, tmp_path / "o.wpilog")
    assert not (tmp_path / "o.wpilog.part").exists()


def test_sync_verifies_then_deletes(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root)
    idx = logsync.Index(tmp_path / "idx.json")
    save = tmp_path / "save"
    files = logsync.list_logs(fs, LOGDIR, [".wpilog", ".hoot"], 60)
    for f in files:
        orig = (root / f.path.lstrip("/")).read_bytes()
        r = logsync.sync_file(fs, "SC", f, str(save), idx, delete_after=True)
        assert r.ok and r.deleted, r.message
        assert r.local.read_bytes() == orig
        assert not r.local.with_name(r.local.name + ".part").exists()
    assert len(fs.removed) == 3
    assert not (root / "home/systemcore/logs/2026-11-18_10-00-00").exists()  # empty session folder cleaned
    assert (root / "home/systemcore/logs").exists()  # log dir itself kept


def test_mismatch_keeps_robot_file(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root, corrupt=True)
    idx = logsync.Index(tmp_path / "idx.json")
    f = logsync.list_logs(fs, LOGDIR, [".wpilog"], 60)[0]
    r = logsync.sync_file(fs, "SC", f, str(tmp_path / "save"), idx, delete_after=True)
    assert not r.ok and not r.deleted and "MISMATCH" in r.message
    assert fs.removed == []
    assert not list((tmp_path / "save").rglob("*.wpilog"))


def test_no_hash_never_deletes(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root, can_hash=False)
    idx = logsync.Index(tmp_path / "idx.json")
    f = logsync.list_logs(fs, LOGDIR, [".wpilog"], 60)[0]
    r = logsync.sync_file(fs, "SC", f, str(tmp_path / "save"), idx, delete_after=True)
    assert r.ok and not r.deleted and fs.removed == []
    assert r.local.exists()


def test_in_use_never_touched(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root)
    f = logsync.RemoteFile(LOGDIR + "/FRC_20261118_090000.wpilog", "FRC_20261118_090000.wpilog", 1000, time.time(), True)
    r = logsync.sync_file(fs, "SC", f, str(tmp_path / "save"), logsync.Index(tmp_path / "i.json"), delete_after=True)
    assert not r.ok and fs.removed == []


def test_cancel_removes_part(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root)
    f = [x for x in logsync.list_logs(fs, LOGDIR, [".wpilog"], 60) if x.size > 100_000][0]
    ev = threading.Event()
    ev.set()
    with pytest.raises(logsync.Cancelled):
        logsync.download(fs, f, tmp_path / "out.wpilog", cancel=ev)
    assert not (tmp_path / "out.wpilog.part").exists()
    assert fs.removed == []


def test_speed_limit(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root)
    f = [x for x in logsync.list_logs(fs, LOGDIR, [".wpilog"], 60) if x.size > 100_000][0]
    t = time.monotonic()
    logsync.download(fs, f, tmp_path / "o.wpilog", speed_limit_kbps=1000)  # 300 KB at ~1 MB/s
    assert time.monotonic() - t > 0.15


def test_already_downloaded_then_delete(tmp_path):
    root = make_robot(tmp_path)
    fs = LocalFS(root)
    idx = logsync.Index(tmp_path / "idx.json")
    f = logsync.list_logs(fs, LOGDIR, [".wpilog"], 60)[0]
    r1 = logsync.sync_file(fs, "SC", f, str(tmp_path / "s"), idx, delete_after=False)
    assert r1.ok and not r1.deleted
    r2 = logsync.sync_file(fs, "SC", f, str(tmp_path / "s"), idx, delete_after=True)
    assert r2.ok and r2.deleted and r2.local == r1.local


# ------------------------------------------------------------------ gate
def test_gate():
    disabled = RobotState(nt_connected=True, known=True)
    assert gate(disabled, False, manual=False).allowed
    assert not gate(disabled, True, manual=True).allowed  # competition lock
    fms = RobotState(nt_connected=True, known=True, fms_attached=True)
    assert not gate(fms, False, manual=True).allowed
    assert not gate(fms, False, manual=True, transfer=False).allowed
    en = RobotState(nt_connected=True, known=True, enabled=True)
    assert not gate(en, False, manual=True).allowed
    assert gate(en, False, manual=True, transfer=False).allowed  # streams OK while enabled in pits
    unknown = RobotState(nt_connected=False)
    assert not gate(unknown, False, manual=False).allowed
    g = gate(unknown, False, manual=True)
    assert g.allowed and g.needs_confirm


def test_control_word():
    w = decode_control_word(1 | 16 | 32)
    assert w["enabled"] and w["fms_attached"] and w["ds_attached"] and not w["autonomous"]


def test_control_word_struct_2027():
    from hotwire.robotstate import decode_control_word_struct

    # value read from the real SystemCore on 2026-11-19: disabled, no DS
    d = decode_control_word_struct(b"\x00" * 8)
    assert d and not d["enabled"] and not d["fms_attached"] and not d["ds_attached"] and d["mode"] == "unknown"
    # enabled teleop with FMS + DS, opModeHash in the low bits must not leak into the flags
    w = (0xABCDEF12345678) | (2 << 56) | (1 << 58) | (1 << 60) | (1 << 61)
    d = decode_control_word_struct(w.to_bytes(8, "little"))
    assert d["enabled"] and d["fms_attached"] and d["ds_attached"] and not d["estop"] and d["mode"] == "teleoperated"
    w = (3 << 56) | (1 << 59) | (1 << 61)
    d = decode_control_word_struct(w.to_bytes(8, "little"))
    assert d["estop"] and not d["enabled"] and d["mode"] == "utility"
    assert decode_control_word_struct(b"\x01") is None and decode_control_word_struct(5) is None


# ------------------------------------------------------------------ config + analysis
CFG = """
defaults:
  warn_margin: 0.1
signals:
  vel: HOT/Shooter/Vel   # comment kept
  amps: /HOT/Shooter/Amps
checks:
  - name: Shooter
    start_when: vel > 5
    window: 2.0
    expect:
      vel:  { settle_to: 50, tolerance: 2, within: 1.0, overshoot_max: 10 }
      amps: { peak_max: 80, steady: [5, 20] }
  - name: Shooter again
    start_when: vel > 5
    window: 1.0
    expect:
      amps: { peak_max: 80 }
"""


def shooter_run(t0=1.0, target=50.0, tau=0.2, amps_steady=12.0, amps_peak=60.0, hz=100):
    rec = {"/HOT/Shooter/Vel": [], "/HOT/Shooter/Amps": []}
    import math

    for i in range(int(4 * hz)):
        t = i / hz
        v = 0.0 if t < t0 else target * (1 - math.exp(-(t - t0) / tau))
        a = 0.0 if t < t0 else amps_peak * math.exp(-(t - t0) / 0.1) + amps_steady
        rec["/HOT/Shooter/Vel"].append((t, v))
        rec["/HOT/Shooter/Amps"].append((t, a))
    return rec


def test_config_ok_and_errors():
    cfg = pc.load_text(CFG)
    assert not cfg.errors, cfg.errors
    assert cfg.topic("vel") == "/HOT/Shooter/Vel"
    bad = pc.load_text(CFG.replace("vel > 5", "vel >> 5", 1).replace("peak_max: 80, steady", "peak_mx: 80, steady"))
    assert any("start_when" in e for e in bad.errors)
    assert any("peak_mx" in e for e in bad.errors)
    assert pc.load_text("checks: [").errors


def test_analyze_pass():
    cfg = pc.load_text(CFG)
    res = an.analyze(cfg, shooter_run(), final=True)
    assert res[0].status == "pass", an.report_text(res)
    # second check needs a NEW rising edge after the first: there isn't one
    assert res[1].status == "missing"


def test_analyze_fail_high_current_and_slow():
    cfg = pc.load_text(CFG)
    res = an.analyze(cfg, shooter_run(tau=0.6, amps_steady=30), final=True)
    kinds = {(m.signal, m.kind): m.status for m in res[0].metrics}
    assert kinds[("amps", "steady")] == "fail"
    assert kinds[("vel", "settle")] in ("warn", "fail")
    assert res[0].status == "fail"


def test_analyze_never_reaches_target():
    cfg = pc.load_text(CFG)
    res = an.analyze(cfg, shooter_run(target=40), final=True)
    m = next(m for m in res[0].metrics if m.kind == "settle")
    assert m.status == "fail" and m.detail == "never settled"


def test_live_running_then_done():
    cfg = pc.load_text(CFG)
    rec = shooter_run()
    res = an.analyze(cfg, rec, data_end=1.5, final=False)
    assert res[0].status == "running"
    assert res[1].status == "waiting"
    res = an.analyze(cfg, rec, data_end=3.5, final=False)
    assert res[0].status == "pass"


def test_live_peak_fails_early():
    cfg = pc.load_text(CFG)
    res = an.analyze(cfg, shooter_run(amps_peak=200), data_end=1.3, final=False)
    assert res[0].status == "fail"


def test_learn_and_apply_keeps_comments():
    cfg = pc.load_text(CFG)
    sugg = an.learn(cfg, shooter_run(amps_steady=10))
    keys = {(s.check_name, s.signal, s.key) for s in sugg}
    assert ("Shooter", "amps", "steady") in keys and ("Shooter", "amps", "peak_max") in keys
    assert not any(s.key in ("settle_to", "final", "tolerance") for s in sugg)  # targets never learned
    new = an.apply_suggestions(CFG, sugg)
    assert "# comment kept" in new
    cfg2 = pc.load_text(new)
    assert not cfg2.errors
    lo, hi = cfg2.checks[0].expect["amps"]["steady"]
    assert lo < 10 < hi
    assert an.analyze(cfg2, shooter_run(amps_steady=10), final=True)[0].status == "pass"


def test_example_config_is_valid():
    p = Path(__file__).parents[1] / "examples" / "pit_checks.yaml"
    cfg = pc.load(p)
    assert not cfg.errors, cfg.errors
    assert len(cfg.checks) >= 4


def test_example_config_against_fake_robot_signals():
    """The fake robot's simulated pit test should pass the example checks."""
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
    from fake_robot import pit_signals

    cfg = pc.load(Path(__file__).parents[1] / "examples" / "pit_checks.yaml")
    rec: dict = {}
    for i in range(0, 3000):
        t = i / 100
        for k, v in pit_signals(t).items():
            rec.setdefault(k, []).append((t, v))
    res = an.analyze(cfg, rec, final=True)
    assert all(r.status in ("pass", "warn") for r in res), an.report_text(res)


# ------------------------------------------------------------------ wpilog
def _wpilog(entries: list[tuple[int, str, str]], records: list[tuple[int, int, bytes]]) -> bytes:
    def rec(eid, ts, payload):
        return bytes([0b0111_1111]) + struct.pack("<IIQ", eid, len(payload), ts) + payload

    def s(x):
        b = x.encode()
        return struct.pack("<I", len(b)) + b

    out = b"WPILOG" + struct.pack("<HI", 0x0100, 0)
    for eid, name, typ in entries:
        out += rec(0, 0, b"\x00" + struct.pack("<I", eid) + s(name) + s(typ) + s(""))
    for eid, ts, payload in records:
        out += rec(eid, ts, payload)
    return out


def test_wpilog_reader(tmp_path):
    data = _wpilog(
        [(1, "NT:/HOT/Shooter/Vel", "double"), (2, "NT:/HOT/Other", "boolean"), (3, "NT:/x", "string")],
        [(1, 1_000_000, struct.pack("<d", 5.5)), (2, 1_500_000, b"\x01"), (1, 2_000_000, struct.pack("<d", 7.0)), (3, 0, b"hi")],
    )
    p = tmp_path / "t.wpilog"
    p.write_bytes(data + b"\x7f\x01")  # truncated tail like a log still being written
    rec, types = read_wpilog(p)
    assert rec["/HOT/Shooter/Vel"] == [(1.0, 5.5), (2.0, 7.0)]
    assert rec["/HOT/Other"] == [(1.5, 1.0)]
    assert "/x" not in rec
    rec2, _ = read_wpilog(p, ["HOT/Shooter/Vel"])
    assert list(rec2) == ["/HOT/Shooter/Vel"]
