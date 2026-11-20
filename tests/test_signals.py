"""Signals tab: data helpers, plus the plot's mouse gestures on a loaded recording."""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("HOTWIRE_HOME", tempfile.mkdtemp(prefix="hotwire_sig_"))

import numpy as np  # noqa: E402

from hotwire import signals as sig  # noqa: E402

# ------------------------------------------------------------ data


def test_live_buffer_append_trim_and_order():
    b = sig.LiveBuffer(keep_s=10)
    b.append("/a", 0.0, 1.0)  # not watched: ignored
    b.watch("/a")
    v0 = b.version("/a")
    for i in range(200):
        b.append("/a", i * 0.1, float(i))
    b.append("/a", 5.05, 999.0)  # out of order goes in its place
    b.append("/a", 19.95, True)  # booleans -> 1.0
    b.append("/a", 20.0, "text")  # not a number: ignored
    t, v = b.arrays("/a")
    assert b.version("/a") > v0
    assert np.all(np.diff(t) >= 0)
    assert t[-1] - t[0] <= 10 * 1.2 + 1e-9  # trimmed
    assert v[-1] == 1.0
    b.unwatch("/a")
    assert b.arrays("/a")[0].size == 0


def test_value_at_and_window_stats():
    t = np.array([1.0, 2.0, 3.0])
    v = np.array([10.0, 20.0, 5.0])
    assert sig.value_at(t, v, 0.5) is None
    assert sig.value_at(t, v, 1.0) == 10.0
    assert sig.value_at(t, v, 2.9) == 20.0  # sample-and-hold
    assert sig.value_at(t, v, 9.0) == 5.0
    assert sig.window_stats(t, v, 1.5, 2.5) == (10.0, 20.0)  # includes the held value at the start
    assert sig.window_stats(np.empty(0), np.empty(0), 0, 1) is None


def test_build_tree_and_filter():
    tree = sig.build_tree(["/HOT/Drive/A", "/HOT/Drive/B", "/HOT/X", "/HOT/X/Y"])
    assert tree["HOT"]["Drive"] == {"A": "/HOT/Drive/A", "B": "/HOT/Drive/B"}
    assert tree["HOT"]["X"] == {"": "/HOT/X", "Y": "/HOT/X/Y"}  # leaf that is also a folder
    assert sig.matches("/HOT/Shooter/Left Top/ShooterLeft TopVelocity", "shoot vel")
    assert not sig.matches("/HOT/Shooter/Hood/ShooterHoodPosition", "shoot vel")


def test_units_only_from_properties(tmp_path):
    from hotwire.nt4 import unit_of

    assert unit_of({"unit": "meter"}) == "meter"
    assert unit_of('{"unit": "amp", "persistent": false}') == "amp"  # pyntcore gives a JSON string
    assert unit_of({}) == "" and unit_of("") == "" and unit_of("not json") == "" and unit_of({"unit": 5}) == ""
    # .wpilog: DataLogManager stores the topic properties as the entry metadata
    import struct

    def s(x):
        return struct.pack("<I", len(x.encode())) + x.encode()

    def rec(eid, ts, payload):
        return bytes([0b0111_1111]) + struct.pack("<IIQ", eid, len(payload), ts) + payload

    data = b"WPILOG" + struct.pack("<HI", 0x0100, 0)
    data += rec(0, 0, b"\x00" + struct.pack("<I", 1) + s("NT:/HOT/Arm/Angle") + s("double") + s('{"unit":"degree"}'))
    data += rec(0, 0, b"\x00" + struct.pack("<I", 2) + s("NT:/HOT/Arm/Current") + s("double") + s(""))
    data += rec(1, 1_000_000, struct.pack("<d", 12.5)) + rec(2, 1_000_000, struct.pack("<d", 3.0))
    p = tmp_path / "u.wpilog"
    p.write_bytes(data)
    log = sig.load_file(p)
    assert log.units == {"/HOT/Arm/Angle": "degree"}  # no unit = no entry (never guessed)


def test_layout_roundtrip(tmp_path):
    lay =sig.Layout([sig.StripCfg([sig.TraceCfg("/a", "#ff0000", "R", False)], 120), sig.StripCfg()])
    p = tmp_path / "l" / f"{sig.safe_name('Shooter: tuning?')}.json"
    sig.save_layout(lay, p)
    assert sig.load_layout(p) == lay
    assert lay.next_color() != "#ff0000"


def test_load_csv_recording(tmp_path):
    from hotwire.pitcheck.recorder import save_csv

    p = tmp_path / "recording.csv"
    save_csv({"/a": [(10.0, 1.0), (11.0, 2.0)], "/b": [(10.5, 0.0)]}, p)
    log = sig.load_file(p)
    assert (log.t0, log.t1) == (10.0, 11.0)
    assert list(log.series["/a"][1]) == [1.0, 2.0]


# ------------------------------------------------------------ GUI: mouse gestures


def _tab(tmp_path):
    from PySide6.QtWidgets import QApplication

    from hotwire.pitcheck.recorder import save_csv
    from hotwire.settings import Settings
    from hotwire.ui.context import AppContext
    from hotwire.ui.signals_tab import SignalsTab

    app = QApplication.instance() or QApplication([])
    t = np.arange(0, 100, 0.02)
    p = tmp_path / "rec.csv"
    save_csv({"/s/sine": list(zip(t, np.sin(t))), "/s/ramp": list(zip(t, t))}, p)
    s = Settings()
    s.competition_lock = True  # no network in this test
    ctx = AppContext(s)
    tab = SignalsTab(ctx)
    tab.resize(1200, 700)
    tab.show()
    tab.open_file(str(p))
    while tab._loading is not None:
        app.processEvents()
    tab.area.set_layout(sig.Layout())
    tab.area.add_signals(["/s/sine"])
    tab.area.add_signals(["/s/ramp"], tab.area.add_strip())
    tab.fit()
    for _ in range(5):
        app.processEvents()
    return app, tab


def _px(strip, x):
    """Plot time x -> viewport pixel (in the middle of the strip's height)."""
    from PySide6.QtCore import QPoint, QPointF

    vb = strip.vb
    sp = vb.mapViewToScene(QPointF(x, 0))
    vp = strip.mapFromScene(sp)
    return QPoint(vp.x(), strip.viewport().height() // 2)


def _drag(app, strip, button, p1, p2, mods=None):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    w = strip.viewport()
    mods = mods or Qt.KeyboardModifier.NoModifier

    def ev(kind, pos, buttons):
        return QMouseEvent(kind, QPointF(pos), QPointF(w.mapToGlobal(pos)), button, buttons, mods)

    app.sendEvent(w, ev(QMouseEvent.Type.MouseButtonPress, p1, button))
    steps = 6
    for i in range(1, steps + 1):
        p = p1 + (p2 - p1) * i / steps
        app.sendEvent(w, ev(QMouseEvent.Type.MouseMove, p, button))
    app.sendEvent(w, ev(QMouseEvent.Type.MouseButtonRelease, p2, Qt.MouseButton.NoButton))
    app.processEvents()


def test_mouse_gestures(tmp_path):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    app, tab = _tab(tmp_path)
    area = tab.area
    top, bottom = area.strips
    a0, b0 = area.x_range()
    assert a0 <= 0.5 and b0 >= 99, (a0, b0)

    # wheel up = zoom in on time (all strips follow)
    w = top.viewport()
    pos = _px(top, 50)
    ev = QWheelEvent(QPointF(pos), QPointF(w.mapToGlobal(pos)), QPoint(), QPoint(0, 240),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)  # fmt: skip
    app.sendEvent(w, ev)
    app.processEvents()
    a1, b1 = area.x_range()
    assert (b1 - a1) < (b0 - a0) * 0.8
    assert bottom.viewRange()[0] == list(area.x_range()) or np.allclose(bottom.viewRange()[0], area.x_range())

    # left drag = pan time (width unchanged, moves the other way)
    _drag(app, top, Qt.MouseButton.LeftButton, _px(top, a1 + (b1 - a1) * 0.6), _px(top, a1 + (b1 - a1) * 0.4))
    a2, b2 = area.x_range()
    assert np.isclose(b2 - a2, b1 - a1, rtol=1e-3)
    assert a2 > a1

    # right drag = box zoom to that time span
    lo, hi = a2 + (b2 - a2) * 0.25, a2 + (b2 - a2) * 0.5
    _drag(app, bottom, Qt.MouseButton.RightButton, _px(bottom, lo), _px(bottom, hi))
    a3, b3 = area.x_range()
    assert np.isclose(a3, lo, atol=(b2 - a2) * 0.03) and np.isclose(b3, hi, atol=(b2 - a2) * 0.03), (a3, b3, lo, hi)

    # Backspace-style undo goes back to the previous range
    assert area.undo_zoom()
    assert np.allclose(area.x_range(), (a2, b2), atol=1e-6)

    # turning on "2 cursors" puts both cursors on screen
    tab.two_cursors.setChecked(True)
    a, b = area.x_range()
    assert all(c is not None and a <= c <= b for c in (area.cursor[1], area.cursor[2]))
    assert all(s.cursor_lines[2].isVisible() for s in area.strips)
    tab.two_cursors.setChecked(False)
    assert area.cursor[2] is None

    # every strip has vertical grid lines (the time axis stays, numbers only on the bottom strip)
    assert all(s.getPlotItem().getAxis("bottom").isVisible() for s in area.strips)
    assert not top.getPlotItem().getAxis("bottom").style["showValues"]
    assert bottom.getPlotItem().getAxis("bottom").style["showValues"]

    # click = C1, shift+click = C2 (and turns on "2 cursors"), values follow
    from PySide6.QtTest import QTest

    QTest.mouseClick(top.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, _px(top, a2 + 1))
    app.processEvents()
    assert area.cursor[1] is not None and abs(area.cursor[1] - (a2 + 1)) < (b2 - a2) * 0.02
    QTest.mouseClick(top.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, _px(top, a2 + 3))
    app.processEvents()
    assert area.cursor[2] is not None and tab.two_cursors.isChecked()
    rows = {name: it for _, name, it in tab.values._rows}
    c1, c2 = area.cursor[1], area.cursor[2]
    ramp = rows["/s/ramp"]
    assert abs(float(ramp.text(5)) - (c2 - c1)) < 0.05  # ramp: Δvalue == Δt

    # arrow keys step C2 sample by sample
    before = area.cursor[2]
    area.step_cursor(tab.series("/s/ramp")[0], +1)
    assert 0.0 < area.cursor[2] - before <= 0.021

    # fit and layout state survive a save/reload
    tab.fit()
    tab.save_state()
    from hotwire.ui.signals_tab import SignalsTab

    tab2 = SignalsTab(tab.ctx)
    assert tab2.area.to_layout().all_names() == ["/s/sine", "/s/ramp"]
    tab.shutdown()
    tab2.shutdown()


def test_side_panel_pin_and_popout(tmp_path):
    app, tab = _tab(tmp_path)
    left = tab.left
    left.set_pinned(False)
    assert not left.frame.isVisible() and left.edge.isVisible()
    left.open()
    app.processEvents()
    assert left.frame.isVisible()
    assert left.frame.geometry().left() == tab.splitter.geometry().left()  # over the plot, not resizing it
    left.close()
    assert not left.frame.isVisible()
    left.set_pinned(True)
    app.processEvents()
    assert tab.splitter.indexOf(left.frame) == 0 and left.frame.isVisible() and not left.edge.isVisible()
    left.set_pinned(False)
    assert tab.splitter.indexOf(left.frame) == -1
    tab.shutdown()
