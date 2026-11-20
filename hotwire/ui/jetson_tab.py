"""Jetson tab: live health (while visible) and snapshots (journal, config, report)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from hotwire import jetson, sshconn
from hotwire.jetson import HeartbeatWatch
from hotwire.settings import data_dir
from hotwire.streams import http_get_json
from hotwire.ui.common import Job, STATUS_COLORS

STATE_FILE = "jetson_state.json"
NT_ROOT = "/HOTVision"
EVENT_TOPIC = f"{NT_ROOT}/events/last_event"


def _load_state() -> dict:
    try:
        return json.loads((data_dir() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(d: dict) -> None:
    (data_dir() / STATE_FILE).write_text(json.dumps(d, indent=1), encoding="utf-8")


class JetsonTab(QWidget):
    FIELDS = [
        ("service", "HOTVision service"),
        ("restarts", "Service restarts"),
        ("nt", "HOTVision NT connected"),
        ("cpu", "CPU load"),
        ("gpu", "GPU load"),
        ("temp", "Hottest sensor"),
        ("mem", "Memory used"),
        ("disk", "Disk used"),
        ("usb", "USB errors (this boot)"),
        ("uptime", "Uptime"),
    ]
    HB_FIELDS = [
        ("alive", "Heartbeat"),
        ("vin", "Input voltage"),
        ("vin_min", "Lowest input voltage seen"),
        ("throttled", "Throttled (heat / over-current)"),
        ("temps", "CPU / GPU temperature"),
        ("reboots", "While watching"),
        ("event_count", "Events since HOTVision started"),
        ("usb_disc", "Camera USB disconnects"),
    ]
    event_arrived = Signal(int, str)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.job: Job | None = None
        self.state = _load_state()

        top = QHBoxLayout()
        top.addWidget(QLabel("Device:"))
        self.dev_combo = QComboBox()
        top.addWidget(self.dev_combo)
        top.addStretch(1)
        self.updated = QLabel("")
        # long SSH errors must not make the tab wider than the window (full text in the tooltip)
        self.updated.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.updated.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.updated, 1)

        box = QGroupBox("Health (updates every 5 s while this tab is open)")
        form = QFormLayout(box)
        self.vals: dict[str, QLabel] = {}
        for key, label in self.FIELDS:
            v = QLabel("-")
            self.vals[key] = v
            form.addRow(label + ":", v)
        self.temps = QLabel("")
        self.temps.setWordWrap(True)
        form.addRow("All temperatures:", self.temps)

        snap = QGroupBox("Snapshot: HOTVision journal, kernel log, camera_config.json, system report")
        sl = QHBoxLayout(snap)
        self.since = QComboBox()
        self.since.addItems(["Since last snapshot", "This boot", "Last hour", "Last 24 hours"])
        sl.addWidget(self.since)
        b = QPushButton("Take snapshot")
        b.clicked.connect(lambda: self.snapshot(manual=True))
        sl.addWidget(b)
        self.snap_label = QLabel(self._last_snap_text())
        sl.addWidget(self.snap_label, 1)
        b_open = QPushButton("Open folder")
        b_open.clicked.connect(self._open_last)
        sl.addWidget(b_open)

        # Heartbeat: published to the robot's NT by HOTVision (jetson-heartbeat branch)
        hb = QGroupBox("Heartbeat (from HOTVision over NetworkTables)")
        hl = QHBoxLayout(hb)
        hform = QFormLayout()
        self.hb_vals: dict[str, QLabel] = {}
        for key, label in self.HB_FIELDS:
            v = QLabel("-")
            self.hb_vals[key] = v
            hform.addRow(label + ":", v)
        hl.addLayout(hform, 1)
        ev = QVBoxLayout()
        ev.addWidget(QLabel("Events (newest first):"))
        self.events = QListWidget()
        self.events.setMinimumWidth(420)
        ev.addWidget(self.events)
        hl.addLayout(ev, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(box)
        lay.addWidget(hb, 1)
        lay.addWidget(snap)

        self.hb = HeartbeatWatch()
        self._hb_uid = None
        self._event_seen: set = set()
        self.event_arrived.connect(self._add_event)
        self.hb_timer = QTimer(self)
        self.hb_timer.timeout.connect(self._show_heartbeat)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        ctx.settings_changed.connect(self._load_devices)
        ctx.robot_connected.connect(self._on_robot_connected)
        self._load_devices()

    def _load_devices(self):
        self.dev_combo.clear()
        for d in self.ctx.settings.devices_of("jetson"):
            self.dev_combo.addItem(d.name)

    def device(self):
        return self.ctx.settings.device(self.dev_combo.currentText())

    def showEvent(self, e):
        self.timer.start(5000)
        QTimer.singleShot(100, self.poll)
        if self._hb_uid is None:
            nt = self.ctx.nt
            self._hb_uid = nt.subscribe([f"{NT_ROOT}/heartbeat/"], prefix=True, periodic=0.25)
            self._ev_uid = nt.subscribe([f"{NT_ROOT}/events/"], prefix=True, send_all=True)
            nt.add_listener(self._on_nt_value)
        self.hb_timer.start(1000)
        self._show_heartbeat()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        self.hb_timer.stop()
        if self._hb_uid is not None:
            nt = self.ctx.nt
            nt.remove_listener(self._on_nt_value)
            nt.unsubscribe(self._hb_uid)
            nt.unsubscribe(self._ev_uid)
            self._hb_uid = None
        super().hideEvent(e)

    # ------------------------------------------------------------ heartbeat (NT)
    def _on_nt_value(self, name, ts, value):
        # NT thread: hand events to the UI thread
        if name == EVENT_TOPIC and isinstance(value, str):
            self.event_arrived.emit(int(ts), value)

    def _add_event(self, ts: int, text: str):
        key = (ts, text)
        if key in self._event_seen:  # the server re-sends the last value on (re)subscribe
            return
        self._event_seen.add(key)
        self.events.insertItem(0, f"{datetime.now():%H:%M:%S}  {text}")
        while self.events.count() > 500:
            self.events.takeItem(self.events.count() - 1)

    def _show_heartbeat(self):
        nt = self.ctx.nt
        get = lambda k: nt.get(f"{NT_ROOT}/heartbeat/{k}")  # noqa: E731
        seq = nt.get_timestamped(f"{NT_ROOT}/heartbeat/seq")
        if seq is None:
            for v in self.hb_vals.values():
                v.setText("-")
                v.setStyleSheet("")
            self.hb_vals["alive"].setText(
                "not published (robot NT not connected, or HOTVision is older than the jetson-heartbeat branch)"
                if nt.connected else "robot NT not connected"
            )
            return
        r = self.hb.update(
            seq_ts_us=seq[0],
            now_us=nt.server_time_us(),
            jetson_uptime=get("jetson_uptime_s"),
            app_uptime=get("app_uptime_s"),
            vin=get("vin_v"),
            vin_min=get("vin_min_v"),
        )
        self._hb_set("alive", r.alive_text, r.alive_status)
        self._hb_set("vin", r.vin_text, r.vin_status)
        self._hb_set("vin_min", r.vin_min_text, r.vin_min_status)
        thr = get("throttled")
        self._hb_set("throttled", {True: "YES", False: "no"}.get(thr, "?"), {True: "fail", False: "pass"}.get(thr, "info"))
        temps = [get("cpu_temp_c"), get("gpu_temp_c")]
        self._hb_set("temps", " / ".join("?" if t is None else f"{t:.0f} °C" for t in temps), "info")
        self._hb_set("reboots", r.reboots_text, "warn" if r.reboots or r.restarts else "pass")
        n = nt.get(f"{NT_ROOT}/events/event_count")
        self._hb_set("event_count", "?" if n is None else f"{n:.0f}", "info")
        u = nt.get(f"{NT_ROOT}/events/usb_disconnects")
        self._hb_set("usb_disc", "?" if u is None else f"{u:.0f}", "info" if u is None else "pass" if u == 0 else "warn")

    def _hb_set(self, key, text, status):
        v = self.hb_vals[key]
        v.setText(text)
        v.setStyleSheet(f"color:{STATUS_COLORS[status]}; font-weight:600;")

    # ------------------------------------------------------------ health
    def poll(self):
        dev = self.device()
        if dev is None or self.job is not None or self.window().isMinimized():
            return
        g = self.ctx.gate(manual=False, transfer=False)
        if not g.allowed:
            self.updated.setText(f"Paused: {g.reason}")
            return
        s = self.ctx.settings

        def work(job):
            with sshconn.connect(dev, s) as conn:
                h = jetson.health(conn, dev)
            try:
                nt = http_get_json(dev, s, "/api/nt/status")
                h["nt"] = bool(nt.get("connected"))
            except Exception:
                h["nt"] = None
            return h

        def done(h):
            self.job = None
            self._show(h)

        def fail(err):
            self.job = None
            self.updated.setText(err.splitlines()[0][:160])
            self.updated.setToolTip(err)

        self.job = Job(work).start(done, fail)

    def _set(self, key, text, status="pass"):
        v = self.vals[key]
        v.setText(text)
        v.setStyleSheet(f"color:{STATUS_COLORS[status]}; font-weight:600;")

    def _show(self, h: dict):
        def pct(x, warn, fail):
            if x is None:
                return "?", "info"
            return f"{x:.0f} %", "fail" if x >= fail else "warn" if x >= warn else "pass"

        self._set("service", h["service"], "pass" if h["service"] == "active" else "fail")
        r = h["restarts"]
        self._set("restarts", str(r), "pass" if r in ("0", "") else "warn")
        nt = h.get("nt")
        self._set("nt", {True: "yes", False: "NO", None: "?"}[nt], {True: "pass", False: "fail", None: "info"}[nt])
        self._set("cpu", *pct(h["cpu_pct"], 85, 95))
        self._set("gpu", *pct(h["gpu_pct"], 90, 99))
        self._set("mem", *pct(h["mem_pct"], 85, 95))
        self._set("disk", *pct(h["disk_pct"], 85, 95))
        temps = h["temps"]
        if temps:
            name, t = max(temps.items(), key=lambda kv: kv[1])
            # Orin NX passive (throttle) trip is 70 °C
            self._set("temp", f"{t:.1f} °C ({name})", "fail" if t >= 70 else "warn" if t >= 63 else "pass")
            self.temps.setText(", ".join(f"{k} {v:.0f}°" for k, v in sorted(temps.items())))
        u = h["usb_errors"]
        self._set("usb", str(u), "pass" if u in ("0", "") else "info" if u == "?" else "warn")
        self.vals["usb"].setToolTip(
            f"Last one: {h['usb_last']}" if h.get("usb_last") else "Disconnects, over-current, descriptor read "
            "errors and resets after boot. The harmless fusb301 boot message is not counted."
        )
        up = h.get("uptime_s")
        self._set("uptime", str(timedelta(seconds=int(up))) if up else "?", "info")
        self.updated.setText(f"Updated {datetime.now():%H:%M:%S}")
        self.updated.setToolTip("")

    # ------------------------------------------------------------ snapshot
    def _since_arg(self) -> str | None:
        i = self.since.currentIndex()
        dev = self.device()
        if i == 0:
            return self.state.get(f"last_snapshot_{dev.name}") if dev else None
        if i == 1:
            return None
        hours = 1 if i == 2 else 24
        return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    def _on_robot_connected(self):
        if self.ctx.settings.jetson_snapshot_on_connect:
            QTimer.singleShot(5000, lambda: self.snapshot(manual=False))

    def snapshot(self, manual: bool):
        dev = self.device()
        if dev is None:
            return
        g = self.ctx.gate(manual=manual, transfer=True)
        if not g.allowed:
            if manual:
                QMessageBox.warning(self, "Not allowed now", g.reason)
            return
        if g.needs_confirm and manual:
            if QMessageBox.question(self, "Robot state unknown", "Only continue in the pits. Continue?") != (
                QMessageBox.StandardButton.Yes
            ):
                return
        since = self._since_arg() if manual else self.state.get(f"last_snapshot_{dev.name}")
        s = self.ctx.settings
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def work(job):
            with sshconn.connect(dev, s) as conn:
                # use the Jetson's clock for "since last snapshot"
                rc, out, _ = conn.run("date '+%Y-%m-%d %H:%M:%S'")
                folder = jetson.take_snapshot(conn, dev, s.save_folder, since)
                return folder, out.strip() or started

        def done(res):
            folder, jetson_now = res
            self.state[f"last_snapshot_{dev.name}"] = jetson_now
            self.state[f"last_folder_{dev.name}"] = str(folder)
            _save_state(self.state)
            self.snap_label.setText(self._last_snap_text())
            self.ctx.log(f"Jetson snapshot saved: {folder}")

        Job(work).start(done, lambda e: (self.ctx.log(f"Jetson snapshot failed: {e}"), manual and QMessageBox.warning(self, "Jetson", e)))

    def _last_snap_text(self) -> str:
        dev = self.device() if hasattr(self, "dev_combo") else None
        if dev is None:
            return ""
        t = self.state.get(f"last_snapshot_{dev.name}")
        return f"Last snapshot: {t}" if t else "No snapshot yet"

    def _open_last(self):
        dev = self.device()
        if dev is None:
            return
        f = self.state.get(f"last_folder_{dev.name}")
        if f and os.path.isdir(f):
            os.startfile(f)  # noqa: S606
