"""Cameras tab: live HOTVision streams plus stream-quality and camera status.

Streams only run while: the tab is visible, the window isn't minimized,
"Streams on" is ticked and the gate allows it. Anything else stops them,
and HOTVision stops encoding as soon as the connection closes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hotwire import jetson, sshconn
from hotwire.streams import StreamReader, StreamStats
from hotwire.ui.common import Job, STATUS_COLORS

NT_ROOT = "/HOTVision"


class Tile(QFrame):
    clicked = Signal(str)

    def __init__(self, cam: str):
        super().__init__()
        self.cam = cam
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title = QLabel(f"<b>{cam}</b>")
        self.pipe = QComboBox()
        self.img = QLabel("stream off")
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setMinimumSize(320, 200)
        self.img.setStyleSheet("background:#111; color:#888;")
        self.nt_line = QLabel()
        self.stream_line = QLabel()
        self.flags = QLabel()
        self.flags.setWordWrap(True)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.pipe)
        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.img, 1)
        lay.addWidget(self.nt_line)
        lay.addWidget(self.stream_line)
        lay.addWidget(self.flags)
        self.reader: StreamReader | None = None
        self.nt_problems: list[str] = []
        self.stream_problems: list[str] = []

    def mousePressEvent(self, e):
        self.clicked.emit(self.cam)

    def show_frame(self, img: QImage):
        self.img.setPixmap(
            QPixmap.fromImage(img).scaled(
                self.img.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )

    def show_stats(self, st: StreamStats, sharp_min: float):
        p = []
        if st.error:
            p.append(f"stream error: {st.error}")
        if st.stalled:
            p.append("STALLED (no frames)")
        if st.frozen:
            p.append("FROZEN picture")
        if st.dark:
            p.append("too DARK")
        if st.washed:
            p.append("WASHED OUT")
        if sharp_min > 0 and st.sharpness < sharp_min and st.frames > 10:
            p.append("BLURRY / out of focus")
        self.stream_problems = p
        self.stream_line.setText(
            f"stream {st.fps:4.1f} fps · brightness {st.brightness:3.0f} · sharpness {st.sharpness:5.0f}"
            + (f" · reconnects {st.reconnects}" if st.reconnects else "")
        )
        self._show_flags()

    def _show_flags(self):
        probs = self.nt_problems + self.stream_problems
        if probs:
            self.flags.setText("⚠ " + " · ".join(probs))
            self.flags.setStyleSheet(f"color:{STATUS_COLORS['fail']}; font-weight:600;")
        else:
            self.flags.setText("✓ OK")
            self.flags.setStyleSheet(f"color:{STATUS_COLORS['pass']}; font-weight:600;")

    def stop(self):
        if self.reader:
            self.reader.stop()
            self.reader = None
            self.img.setText("stream off")


class CamerasTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.tiles: dict[str, Tile] = {}
        self.solo: str | None = None
        self.config_fps: dict[str, float] = {}
        self._confirmed_unknown = False

        top = QHBoxLayout()
        self.cb_streams = QCheckBox("Streams on")
        self.cb_streams.toggled.connect(self._streams_toggled)
        top.addWidget(self.cb_streams)
        self.dev_combo = QComboBox()
        top.addWidget(QLabel("Jetson:"))
        top.addWidget(self.dev_combo)
        b_cfg = QPushButton("Read camera config")
        b_cfg.setToolTip("Reads the configured fps for each camera from the Jetson (SSH)")
        b_cfg.clicked.connect(self.read_config)
        top.addWidget(b_cfg)
        self.b_all = QPushButton("Show all cameras")
        self.b_all.clicked.connect(lambda: self._set_solo(None))
        self.b_all.setVisible(False)
        top.addWidget(self.b_all)
        top.addStretch(1)
        self.info = QLabel("Click a camera to stream only that one (less load on the Jetson).")
        top.addWidget(self.info)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(scroll, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        ctx.settings_changed.connect(self._load_devices)
        ctx.lock_changed.connect(lambda on: on and self.stop_all())
        self._load_devices()

    def _load_devices(self):
        self.dev_combo.clear()
        for d in self.ctx.settings.devices_of("jetson"):
            self.dev_combo.addItem(d.name)

    def device(self):
        return self.ctx.settings.device(self.dev_combo.currentText())

    # ------------------------------------------------------------ visibility
    def showEvent(self, e):
        # NT4 only announces subscribed topics: subscribe to HOTVision while visible
        if getattr(self, "_nt_uid", None) is None:
            self._nt_uid = self.ctx.nt.subscribe([NT_ROOT + "/"], prefix=True, periodic=0.25)
        super().showEvent(e)

    def hideEvent(self, e):
        self.stop_all()
        if getattr(self, "_nt_uid", None) is not None:
            self.ctx.nt.unsubscribe(self._nt_uid)
            self._nt_uid = None
        super().hideEvent(e)

    def window_minimized(self, minimized: bool):
        if minimized:
            self.stop_all()

    def stop_all(self):
        for t in self.tiles.values():
            t.stop()

    def _streams_toggled(self, on: bool):
        if not on:
            self.stop_all()
            self._confirmed_unknown = False
            return
        g = self.ctx.gate(manual=True, transfer=False)
        if not g.allowed:
            QMessageBox.warning(self, "Not allowed now", g.reason)
            self.cb_streams.setChecked(False)
            return
        if g.needs_confirm and not self._confirmed_unknown:
            r = QMessageBox.question(
                self, "Robot state unknown", "Can't read the robot's state. Only stream in the pits. Continue?"
            )
            if r != QMessageBox.StandardButton.Yes:
                self.cb_streams.setChecked(False)
                return
            self._confirmed_unknown = True

    def _set_solo(self, cam: str | None):
        self.solo = cam
        self.b_all.setVisible(cam is not None)
        for name, t in self.tiles.items():
            t.setVisible(cam is None or name == cam)
            if cam is not None and name != cam:
                t.stop()

    # ------------------------------------------------------------ camera list (NT)
    def _cameras_from_nt(self) -> dict[str, list[str]]:
        """{camera: [pipelines]} for real cameras.

        Only cameras with a 'connected' topic count: HOTVision also publishes a
        merged 'BACK' entry (BACK_LEFT + BACK_RIGHT) that has no stream.
        """
        cams: dict[str, list[str]] = {}
        real: set[str] = set()
        for t in self.ctx.nt.topics():
            parts = t.name.split("/")
            if len(parts) >= 4 and "/" + parts[1] == NT_ROOT:
                cam = parts[2]
                cams.setdefault(cam, [])
                if len(parts) == 4 and parts[3] == "connected":
                    real.add(cam)
                if len(parts) >= 5 and parts[3] not in cams[cam]:
                    cams[cam].append(parts[3])
        return {c: p for c, p in cams.items() if c in real}

    def _tick(self):
        if not self.isVisible():
            return
        cams = self._cameras_from_nt()
        if not self.ctx.nt.connected:
            self.info.setText("Robot NetworkTables not connected: camera list comes from NT.")
        for cam, pipes in sorted(cams.items()):
            if cam not in self.tiles:
                t = Tile(cam)
                t.clicked.connect(self._set_solo)
                self.tiles[cam] = t
                n = len(self.tiles) - 1
                self.grid.addWidget(t, n // 2, n % 2)
            t = self.tiles[cam]
            if [t.pipe.itemText(i) for i in range(t.pipe.count())] != pipes:
                t.pipe.clear()
                t.pipe.addItems(pipes)
            self._update_nt(t)
        # gate: stop everything the moment it's not allowed
        allowed = self.ctx.gate(manual=True, transfer=False).allowed
        want = self.cb_streams.isChecked() and allowed and not self.window().isMinimized()
        if self.cb_streams.isChecked() and not allowed:
            self.cb_streams.setChecked(False)
            self.ctx.log("Cameras: streams stopped (robot on field / lock)")
        dev = self.device()
        for name, t in self.tiles.items():
            run = want and dev is not None and (self.solo is None or self.solo == name) and t.pipe.count() > 0
            pipe = t.pipe.currentText()
            if run and (t.reader is None or t.reader.pipeline != pipe):
                t.stop()
                r = StreamReader(dev, self.ctx.settings, name, pipe)
                r.frame.connect(t.show_frame)
                r.stats.connect(lambda st, t=t: t.show_stats(st, self.ctx.settings.stream_sharpness_min))
                t.reader = r
                r.start()
            elif not run and t.reader is not None:
                t.stop()

    def _update_nt(self, t: Tile):
        nt = self.ctx.nt
        base = f"{NT_ROOT}/{t.cam}"
        connected = nt.get(f"{base}/connected")
        cap = nt.get(f"{base}/capture_fps")
        pipe = t.pipe.currentText() or "apriltag"
        lat = nt.get(f"{base}/{pipe}/latency")
        tags = nt.get(f"{base}/{pipe}/num_tags")
        want_fps = self.config_fps.get(t.cam)
        probs = []
        if connected is False:
            probs.append("camera NOT CONNECTED")
        if cap is not None and want_fps and cap < 0.8 * want_fps:
            probs.append(f"capture fps low ({cap:.0f} of {want_fps:.0f})")
        t.nt_problems = probs
        parts = [f"NT: {'connected' if connected else 'not connected' if connected is False else '?'}"]
        if cap is not None:
            parts.append(f"capture {cap:.1f}" + (f"/{want_fps:.0f}" if want_fps else "") + " fps")
        if lat is not None:
            parts.append(f"latency {float(lat):.0f} ms")
        if tags is not None:
            parts.append(f"tags {int(tags)}")
        t.nt_line.setText(" · ".join(parts))
        t._show_flags()

    # ------------------------------------------------------------ config fps
    def read_config(self):
        dev = self.device()
        if dev is None:
            return
        g = self.ctx.gate(manual=True, transfer=False)
        if not g.allowed:
            QMessageBox.warning(self, "Not allowed now", g.reason)
            return

        def work(job):
            with sshconn.connect(dev, self.ctx.settings) as conn:
                return jetson.read_camera_config(conn, dev)

        def done(fps):
            self.config_fps = fps
            self.ctx.log(f"Cameras: configured fps {fps}")

        Job(work).start(done, lambda e: QMessageBox.warning(self, "Jetson", e))
