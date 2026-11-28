"""Cameras tab: live HOTVision streams, camera status, and image tuning.

Left: one tile per camera, sized so all of them fill the tab's height (2 columns
for up to 4 cameras, 3 for 5-6). Right: the selected camera's settings
(exposure, gamma, contrast, brightness), Tune / Keep / Undo, and a before/now
comparison.

Streams only run while: the tab is visible, the window isn't minimized,
"Streams on" is ticked and the gate allows it. Anything else stops them,
and HOTVision stops encoding as soon as the connection closes.

Tuning (hotwire/camtune.py) runs from the laptop through HOTVision's API and
only with the robot disabled and no FMS. HOTVision saves every settings change,
so the settings from before are kept in a "pending" file on the laptop until
Keep or Undo; quitting puts them back, and after a crash HOTWire offers to.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hotwire import camtune as ct
from hotwire import jetson, sshconn
from hotwire.settings import data_dir
from hotwire.streams import StreamReader, StreamStats, http_get_json, http_post_json
from hotwire.ui.common import Job, STATUS_COLORS

NT_ROOT = "/HOTVision"
# Subtables under a camera that aren't pipelines (no stream): camera health from the heartbeat
NOT_PIPELINES = {"status"}
PENDING_FILE = "camera_tune_pending.json"
PANEL_W = 400
TILE_TEXT_H = 118  # title row + 3 text lines + margins under/over the picture


def _settings_url(cam: str) -> str:
    return f"/api/cameras/{cam}/settings"


# ====================================================================== tile
class Tile(QFrame):
    clicked = Signal(str)
    double_clicked = Signal(str)
    aspect_changed = Signal()

    def __init__(self, cam: str):
        super().__init__()
        self.cam = cam
        self.setObjectName("camtile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title = QLabel(f"<b>{cam}</b>")
        self.pipe = QComboBox()
        self.img = QLabel("stream off")
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setMinimumSize(160, 100)
        self.img.setStyleSheet("background:#111; color:#888;")
        self.aspect = 800 / 1280  # height / width, updated from the first frame
        self.nt_line = QLabel()
        self.stream_line = QLabel()
        self.flags = QLabel()
        for lab in (self.nt_line, self.stream_line, self.flags):
            # long text must not widen the tile (the full text is in the tooltip)
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.pipe)
        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.img, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.nt_line)
        lay.addWidget(self.stream_line)
        lay.addWidget(self.flags)
        self.reader: StreamReader | None = None
        self.nt_problems: list[str] = []
        self.stream_problems: list[str] = []
        self.last_image: QImage | None = None
        self.before_image: QImage | None = None
        self.show_before = False
        self.set_selected(False)

    def mousePressEvent(self, e):
        self.clicked.emit(self.cam)

    def mouseDoubleClickEvent(self, e):
        self.double_clicked.emit(self.cam)

    def set_selected(self, on: bool) -> None:
        color = "#3a6ea5" if on else "transparent"
        self.setStyleSheet(f"QFrame#camtile {{ border: 2px solid {color}; border-radius: 4px; }}")

    def fit(self, pic_w: float, pic_h: float) -> None:
        self.img.setFixedSize(max(160, int(pic_w)), max(100, int(pic_h)))
        self._redraw()

    def show_frame(self, img: QImage):
        self.last_image = img
        if img.width() > 0:
            aspect = img.height() / img.width()
            if abs(aspect - self.aspect) > 0.01:
                self.aspect = aspect
                self.aspect_changed.emit()  # the tab re-fits every tile
        if not self.show_before:
            self._draw(img)

    def set_show_before(self, on: bool) -> None:
        self.show_before = on and self.before_image is not None
        self._redraw()

    def _redraw(self):
        if self.show_before:
            self._draw(self.before_image)
        elif self.reader is not None:
            self._draw(self.last_image)

    def _draw(self, img: QImage | None):
        if img is None:
            return
        pm = QPixmap.fromImage(img).scaled(
            self.img.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        if self.show_before:
            from PySide6.QtGui import QColor, QPainter

            p = QPainter(pm)
            p.fillRect(0, 0, 86, 22, QColor(0, 0, 0, 170))
            p.setPen(QColor("#ffd45a"))
            p.drawText(8, 16, "BEFORE")
            p.end()
        self.img.setPixmap(pm)

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
        text = (
            f"stream {st.fps:4.1f} fps · brightness {st.brightness:3.0f} · contrast {st.contrast:3.0f}"
            f" · sharpness {st.sharpness:5.0f}" + (f" · reconnects {st.reconnects}" if st.reconnects else "")
        )
        self.stream_line.setText(text)
        self.stream_line.setToolTip(text)
        self._show_flags()

    def _show_flags(self):
        probs = self.nt_problems + self.stream_problems
        if probs:
            self.flags.setText("⚠ " + " · ".join(probs))
            self.flags.setStyleSheet(f"color:{STATUS_COLORS['fail']}; font-weight:600;")
        else:
            self.flags.setText("✓ OK")
            self.flags.setStyleSheet(f"color:{STATUS_COLORS['pass']}; font-weight:600;")
        self.flags.setToolTip(self.flags.text())

    def stop(self):
        if self.reader:
            self.reader.stop()
            self.reader = None
            self.img.setPixmap(QPixmap())
            self.img.setText("stream off")


# ====================================================================== tuning run state
@dataclass
class CamRun:
    cam: str
    tune: ct.TuneRun
    coll: ct.Collector = field(default_factory=ct.Collector)
    state: str = "next"  # next | applying | settle | measure
    t0: float = 0.0
    applied: dict | None = None  # settings HOTVision has right now
    last_seq: int = -1


# ====================================================================== tab
class CamerasTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.tiles: dict[str, Tile] = {}
        self.solo: str | None = None
        self.selected: str | None = None
        self._nt_uid = None
        self._fast_uid = None
        self.config_fps: dict[str, float] = {}
        self._confirmed_unknown = False
        # tuning
        self.pending = ct.PendingStore(data_dir() / PENDING_FILE)
        self.runs: dict[str, CamRun] = {}
        self.review: dict[str, dict] = {}  # cam -> before/after for the compare table
        self.owned: set[str] = set()  # cams changed by this HOTWire session (not to ask about)
        self.ranges: dict[str, dict] = {}
        self._asked_recover = False
        self._tune_dev = None
        ctx.tick.connect(self._nt_sync)  # drop the subscription while the FMS is attached
        ctx.tick.connect(self._recovery_check)

        # --- top bar
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
        self.info = QLabel("Click a camera to select it; double-click to stream only that one.")
        top.addWidget(self.info)

        # --- left: camera tiles
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.grid_host)

        # --- right: settings + tuning panel (scrolls by itself, so it never makes the tab
        # taller than the window: the camera tiles always fill exactly the visible height)
        panel_scroll = QScrollArea()
        panel_scroll.setWidgetResizable(True)
        panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel_scroll.setWidget(self._build_panel())
        panel_scroll.setFixedWidth(PANEL_W + 18)
        panel_scroll.setMinimumHeight(120)

        body = QHBoxLayout()
        body.addWidget(self.scroll, 1)
        body.addWidget(panel_scroll, 0)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(body, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self.tune_timer = QTimer(self)
        self.tune_timer.setInterval(100)
        self.tune_timer.timeout.connect(self._tune_tick)
        ctx.settings_changed.connect(self._load_devices)
        ctx.lock_changed.connect(lambda on: on and self.stop_all())
        self._load_devices()

    # ------------------------------------------------------------ panel
    def _build_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(PANEL_W)
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Camera:"))
        self.sel_combo = QComboBox()
        self.sel_combo.currentTextChanged.connect(lambda c: self._select(c) if c else None)
        row.addWidget(self.sel_combo, 1)
        v.addLayout(row)

        g = QGroupBox("Settings")
        f = QFormLayout(g)
        self.spins: dict[str, QSpinBox] = {}
        for k in ct.TUNE_KEYS:
            sb = QSpinBox()
            sb.setRange(-100000, 100000)
            sb.setKeyboardTracking(False)
            self.spins[k] = sb
            f.addRow(ct.LABELS[k] + ":", sb)
        br = QHBoxLayout()
        self.b_read = QPushButton("Read from camera")
        self.b_read.clicked.connect(self.read_settings)
        self.b_apply = QPushButton("Apply")
        self.b_apply.setToolTip("Send these settings to the camera (Keep or Undo afterwards)")
        self.b_apply.clicked.connect(self.apply_settings)
        br.addWidget(self.b_read)
        br.addWidget(self.b_apply)
        f.addRow(br)
        v.addWidget(g)

        t = QGroupBox("Tuning")
        tv = QVBoxLayout(t)
        hint = QLabel(
            "Park the robot so each camera sees 2+ tags, robot disabled. Tries exposure, gamma, "
            "contrast and brightness (about 25 s, all cameras at once) and keeps the best."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        tv.addWidget(hint)
        tb = QHBoxLayout()
        self.b_tune = QPushButton("Tune this camera")
        self.b_tune.clicked.connect(lambda: self.start_tuning([self.selected] if self.selected else []))
        self.b_tune_all = QPushButton("Tune all")
        self.b_tune_all.clicked.connect(lambda: self.start_tuning(sorted(self.tiles)))
        self.b_stop = QPushButton("Stop")
        self.b_stop.clicked.connect(lambda: self.abort_tuning("Stopped"))
        for b in (self.b_tune, self.b_tune_all, self.b_stop):
            tb.addWidget(b)
        tv.addLayout(tb)
        self.tune_status = QLabel("")
        self.tune_status.setWordWrap(True)
        tv.addWidget(self.tune_status)

        self.compare = QTableWidget(0, 2)
        self.compare.setHorizontalHeaderLabels(["Before", "Now"])
        self.compare.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.compare.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.compare.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.compare.verticalHeader().setDefaultSectionSize(20)
        self.compare.setMinimumHeight(22 + 13 * 20)  # every row visible (the panel scrolls if needed)
        tv.addWidget(self.compare, 1)

        kb = QHBoxLayout()
        self.b_before = QPushButton("Show before")
        self.b_before.setCheckable(True)
        self.b_before.setToolTip("Show the picture from before tuning in the camera's tile")
        self.b_before.toggled.connect(self._show_before_toggled)
        self.b_keep = QPushButton("Keep")
        self.b_keep.clicked.connect(lambda: self.keep([self.selected]) if self.selected else None)
        self.b_undo = QPushButton("Undo")
        self.b_undo.clicked.connect(lambda: self.undo([self.selected]) if self.selected else None)
        for b in (self.b_before, self.b_keep, self.b_undo):
            kb.addWidget(b)
        tv.addLayout(kb)
        ab = QHBoxLayout()
        self.b_keep_all = QPushButton("Keep all")
        self.b_keep_all.clicked.connect(lambda: self.keep(self._changed_cams()))
        self.b_undo_all = QPushButton("Undo all")
        self.b_undo_all.clicked.connect(lambda: self.undo(self._changed_cams()))
        ab.addWidget(self.b_keep_all)
        ab.addWidget(self.b_undo_all)
        tv.addLayout(ab)
        v.addWidget(t, 1)
        self._refresh_panel()
        return w

    def _changed_cams(self) -> list[str]:
        dev = self.device()
        cams = set(self.review)
        if dev:
            cams |= set(self.pending.cameras(dev.name))
        return sorted(cams - set(self.runs))

    def _load_devices(self):
        self.dev_combo.clear()
        for d in self.ctx.settings.devices_of("jetson"):
            self.dev_combo.addItem(d.name)

    def device(self):
        return self.ctx.settings.device(self.dev_combo.currentText())

    # ------------------------------------------------------------ visibility
    def showEvent(self, e):
        self._nt_sync()
        super().showEvent(e)
        QTimer.singleShot(0, self._fit_tiles)

    def hideEvent(self, e):
        try:
            if self.runs:  # streams stop when the tab is hidden, and tuning needs them
                self.abort_tuning("Stopped: Cameras tab closed")
            self.stop_all()
            self._nt_unsubscribe()
        except RuntimeError:
            pass  # widgets already deleted (app shutting down; shutdown() handled it)
        super().hideEvent(e)

    def _nt_sync(self, _state=None):
        """NT4 only announces subscribed topics: subscribe to HOTVision while visible,
        but not while the robot is on the field (FMS attached)."""
        if self.isVisible() and not self.ctx.state.fms_attached:
            if self._nt_uid is None:
                self._nt_uid = self.ctx.nt.subscribe([NT_ROOT + "/"], prefix=True, periodic=0.25)
        else:
            self._nt_unsubscribe()

    def _nt_unsubscribe(self):
        if self._nt_uid is not None:
            self.ctx.nt.unsubscribe(self._nt_uid)
            self._nt_uid = None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_tiles()

    # ------------------------------------------------------------ layout
    def _visible_tiles(self) -> list[Tile]:
        return [t for name, t in sorted(self.tiles.items()) if self.solo is None or name == self.solo]

    def _relayout(self):
        for t in self.tiles.values():
            self.grid.removeWidget(t)
            t.setVisible(False)
        tiles = self._visible_tiles()
        cols = 1 if len(tiles) <= 1 else 2 if len(tiles) <= 4 else 3
        for i, t in enumerate(tiles):
            self.grid.addWidget(t, i // cols, i % cols)
            t.setVisible(True)
        self._fit_tiles()

    def _fit_tiles(self):
        """All tiles fill the tab's height: pictures as tall as the rows allow, width from the camera's shape."""
        tiles = self._visible_tiles()
        if not tiles:
            return
        cols = 1 if len(tiles) <= 1 else 2 if len(tiles) <= 4 else 3
        rows = math.ceil(len(tiles) / cols)
        vp = self.scroll.viewport()
        m = self.grid.contentsMargins()
        sp = self.grid.spacing()
        avail_h = vp.height() - m.top() - m.bottom() - (rows - 1) * sp
        avail_w = vp.width() - m.left() - m.right() - (cols - 1) * sp
        pic_h_max = avail_h / rows - TILE_TEXT_H
        pic_w_max = avail_w / cols - 24
        for t in tiles:
            pic_h = pic_h_max
            pic_w = pic_h / t.aspect
            if pic_w > pic_w_max:
                pic_w = pic_w_max
                pic_h = pic_w * t.aspect
            t.fit(pic_w, pic_h)

    def window_minimized(self, minimized: bool):
        if minimized:
            if self.runs:
                self.abort_tuning("Stopped: window minimized")
            self.stop_all()

    def stop_all(self):
        for t in self.tiles.values():
            t.stop()

    def _streams_toggled(self, on: bool):
        if not on:
            if self.runs:
                self.abort_tuning("Stopped: streams turned off")
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
        if self.runs and cam is not None:
            return  # tuning needs every tuned camera's stream
        self.solo = cam
        self.b_all.setVisible(cam is not None)
        for name, t in self.tiles.items():
            if cam is not None and name != cam:
                t.stop()
        self._relayout()

    def _toggle_solo(self, cam: str):
        self._set_solo(None if self.solo == cam else cam)

    def _select(self, cam: str):
        if cam not in self.tiles:
            return
        if self.selected and self.selected in self.tiles:
            self.tiles[self.selected].set_show_before(False)
        self.selected = cam
        for name, t in self.tiles.items():
            t.set_selected(name == cam)
        if self.sel_combo.currentText() != cam:
            self.sel_combo.setCurrentText(cam)
        self.b_before.setChecked(False)
        rev = self.review.get(cam)
        if rev and rev.get("after"):
            self._fill_spins(rev["after"])
        self._refresh_panel()

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
                if len(parts) >= 5 and parts[3] not in NOT_PIPELINES and parts[3] not in cams[cam]:
                    cams[cam].append(parts[3])
        return {c: p for c, p in cams.items() if c in real}

    def _tick(self):
        if not self.isVisible():
            return
        cams = self._cameras_from_nt()
        if not self.ctx.nt.connected:
            self.info.setText("Robot NetworkTables not connected: camera list comes from NT.")
        added = False
        for cam, pipes in sorted(cams.items()):
            if cam not in self.tiles:
                t = Tile(cam)
                t.clicked.connect(self._select)
                t.double_clicked.connect(self._toggle_solo)
                t.aspect_changed.connect(self._fit_tiles)
                self.tiles[cam] = t
                self.sel_combo.addItem(cam)
                added = True
            t = self.tiles[cam]
            if [t.pipe.itemText(i) for i in range(t.pipe.count())] != pipes:
                t.pipe.clear()
                t.pipe.addItems(pipes)
            self._update_nt(t)
        if added:
            self._relayout()
            if self.selected is None:
                self._select(sorted(self.tiles)[0])
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
        t.nt_line.setToolTip(t.nt_line.text())
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

    # ================================================================== settings (manual)
    def _fill_spins(self, settings: dict):
        for k, sb in self.spins.items():
            rng = self.ranges.get(self.selected or "", {}).get(k)
            if rng:
                sb.setRange(int(rng[0]), int(rng[1]))
            if k in settings:
                sb.setValue(int(round(settings[k])))

    def _change_gate(self) -> bool:
        """Settings changes: robot disabled, no FMS, no lock (asks if the state is unknown)."""
        g = self.ctx.gate(manual=True, transfer=True)
        if not g.allowed:
            QMessageBox.warning(self, "Not allowed now", g.reason)
            return False
        if g.needs_confirm and not self._confirmed_unknown:
            r = QMessageBox.question(
                self,
                "Robot state unknown",
                "Can't read the robot's state. Only change camera settings in the pits or during "
                "practice setup (robot disabled). Continue?",
            )
            if r != QMessageBox.StandardButton.Yes:
                return False
            self._confirmed_unknown = True
        return True

    def read_settings(self):
        dev, cam = self.device(), self.selected
        if dev is None or not cam:
            return
        if not self.ctx.gate(manual=True, transfer=False).allowed:
            return

        def done(info):
            c = info.get(cam)
            if c is None:
                QMessageBox.warning(self, "Camera", f"HOTVision doesn't know a camera named {cam}.")
                return
            self._fill_spins(c.settings)

        Job(lambda job: ct.parse_cameras(http_get_json(dev, self.ctx.settings, "/api/cameras"))).start(
            done, lambda e: QMessageBox.warning(self, "HOTVision", e)
        )

    def apply_settings(self):
        dev, cam = self.device(), self.selected
        if dev is None or not cam or cam in self.runs or not self._change_gate():
            return
        new = {k: float(sb.value()) for k, sb in self.spins.items()}
        s = self.ctx.settings
        pending = self.pending

        def work(job):
            cur = ct.parse_cameras(http_get_json(dev, s, "/api/cameras")).get(cam)
            if cur is None:
                raise RuntimeError(f"HOTVision doesn't know a camera named {cam}")
            pending.add(dev.name, cam, cur.settings)  # before the first change only
            http_post_json(dev, s, _settings_url(cam), new)
            return pending.get(dev.name, cam)

        def done(before):
            self.owned.add(cam)
            rev = self.review.setdefault(cam, {})
            rev.update(before=before, after=new)
            self.ctx.log(f"Cameras: {cam} settings applied ({_fmt_settings(new)}). Keep or Undo.")
            self._refresh_panel()

        Job(work).start(done, lambda e: QMessageBox.warning(self, "HOTVision", e))

    # ================================================================== tuning
    def start_tuning(self, cams: list[str]):
        cams = [c for c in cams if c]
        dev = self.device()
        if self.runs or not cams:
            return
        if dev is None:
            QMessageBox.warning(self, "Tuning", "No Jetson device in Settings.")
            return
        if not self._change_gate():
            return
        if not self.ctx.nt.connected:
            QMessageBox.warning(self, "Tuning", "Robot NetworkTables isn't connected (tuning scores from NT).")
            return
        self._set_solo(None)
        if not self.cb_streams.isChecked():
            self.cb_streams.setChecked(True)  # the picture measures come from the streams
            if not self.cb_streams.isChecked():
                return
        self.tune_status.setText("Reading camera settings…")
        s = self.ctx.settings
        self._tune_dev = dev

        def work(job):
            info = ct.parse_cameras(http_get_json(dev, s, "/api/cameras"))
            ranges, note = {}, ""
            try:
                with sshconn.connect(dev, s) as conn:
                    ranges = jetson.read_v4l2_ranges(conn, {c: info[c].index for c in cams if c in info})
            except Exception as e:  # tuning still works with estimated ranges
                note = str(e).splitlines()[0][:120]
            return info, ranges, note

        def done(res):
            info, ranges, note = res
            if note:
                self.ctx.log(f"Cameras: couldn't read the cameras' setting ranges over SSH ({note}); estimating them")
            self.ranges.update(ranges)
            for cam in cams:
                c = info.get(cam)
                if c is None or not c.settings:
                    self.ctx.log(f"Cameras: {cam}: not found in HOTVision, skipped")
                    continue
                self.pending.add(dev.name, cam, c.settings)  # saved BEFORE the first change
                before = self.pending.get(dev.name, cam) or c.settings
                self.owned.add(cam)
                run = CamRun(cam, ct.TuneRun(c.settings, ranges.get(cam, {}), c.framerate))
                run.applied = dict(run.tune.before)
                self.runs[cam] = run
                self.review[cam] = {"before": before}
                if cam in self.tiles:
                    self.tiles[cam].before_image = None
            if not self.runs:
                self.tune_status.setText("Nothing to tune.")
                return
            self._fast_uid = self.ctx.nt.subscribe(
                [f"{NT_ROOT}/{c}/" for c in self.runs], prefix=True, periodic=0.05
            )
            self.ctx.log(f"Cameras: tuning {', '.join(sorted(self.runs))}")
            self.tune_timer.start()
            self._refresh_panel()

        Job(work).start(done, lambda e: (self.tune_status.setText(""), QMessageBox.warning(self, "HOTVision", e)))

    def _tune_tick(self):
        if not self.runs:
            self._tuning_stopped()
            return
        g = self.ctx.gate(manual=True, transfer=True)
        if not g.allowed:
            self.abort_tuning(f"Tuning stopped: {g.reason}")
            return
        dev, s, nt = self._tune_dev, self.ctx.settings, self.ctx.nt
        now = time.monotonic()
        for cam, run in list(self.runs.items()):
            if run.state == "next":
                trial = run.tune.next_trial()
                if trial is None:
                    self._finish(cam)
                    continue
                if trial == run.applied:
                    run.state, run.t0 = "settle", now
                else:
                    run.state = "applying"

                    def ok(_res, run=run, trial=trial):
                        if self.runs.get(run.cam) is run:
                            run.applied = trial
                            run.state, run.t0 = "settle", time.monotonic()

                    def bad(err, cam=cam):
                        if cam in self.runs:
                            self._run_failed(cam, err)

                    Job(lambda job, cam=cam, trial=trial: http_post_json(dev, s, _settings_url(cam), trial)).start(
                        ok, bad
                    )
            elif run.state == "settle" and now - run.t0 >= ct.SETTLE_S:
                run.coll.reset()
                tile = self.tiles.get(cam)
                run.last_seq = tile.reader.st.image_seq if tile and tile.reader else -1
                run.state, run.t0 = "measure", now
            elif run.state == "measure":
                base = f"{NT_ROOT}/{cam}"
                tags = nt.get(f"{base}/apriltag/num_tags")
                if isinstance(tags, (int, float)):
                    run.coll.add_tags(tags)
                fps = nt.get(f"{base}/capture_fps")
                if isinstance(fps, (int, float)):
                    run.coll.add_fps(fps)
                amb = nt.get(f"{base}/apriltag/ambiguity")
                if nt.get(f"{base}/apriltag/has_pose") and isinstance(amb, (int, float)):
                    run.coll.add_ambiguity(amb)
                tile = self.tiles.get(cam)
                st = tile.reader.st if tile and tile.reader else None
                if st is not None and st.image_seq != run.last_seq:
                    run.last_seq = st.image_seq
                    run.coll.add_image(st.brightness, st.blown, st.crushed, st.contrast)
                if now - run.t0 >= ct.MEASURE_S:
                    m = run.coll.result()
                    first = run.tune.done_trials == 0
                    run.tune.report(ct.score(m, run.tune.framerate), m)
                    if first and tile is not None and tile.last_image is not None:
                        tile.before_image = tile.last_image.copy()
                    run.state = "next"
        self._refresh_status()
        if int(now * 10) % 3 == 0:
            self._refresh_panel()

    def _finish(self, cam: str):
        run = self.runs.pop(cam)
        tune = run.tune
        rev = self.review.setdefault(cam, {})
        rev.update(
            after=tune.best,
            before_m=tune.before_measure,
            after_m=tune.after_measure,
            before_score=tune.before_score,
            after_score=tune.after_score,
        )
        b, a = tune.before_measure, tune.after_measure
        tags = f"tags {b.tags:.1f} → {a.tags:.1f}" if b and a else ""
        self.ctx.log(f"Cameras: {cam} tuned ({_fmt_settings(tune.best)}) {tags}. Keep or Undo.")
        if cam == self.selected:
            self._fill_spins(tune.best)
        if not self.runs:
            self._tuning_stopped()
            self.ctx.notify.emit("HOTWire", "Camera tuning done: Keep or Undo")
        self._refresh_panel()

    def _tuning_stopped(self):
        self.tune_timer.stop()
        if self._fast_uid is not None:
            self.ctx.nt.unsubscribe(self._fast_uid)
            self._fast_uid = None
        self._refresh_status()

    def _run_failed(self, cam: str, err: str):
        self.runs.pop(cam, None)
        self.ctx.log(f"Cameras: {cam}: tuning failed ({err.splitlines()[0][:120]}); putting the old settings back")
        self.undo([cam], quiet=True)
        if not self.runs:
            self._tuning_stopped()

    def abort_tuning(self, reason: str):
        """Stop tuning; put the old settings back if HOTWire may still talk to the Jetson."""
        if not self.runs:
            return
        cams = sorted(self.runs)
        self.runs.clear()
        self._tuning_stopped()
        self.ctx.log(f"Cameras: {reason} ({', '.join(cams)})")
        if self.ctx.gate(manual=True, transfer=False).allowed:
            self.undo(cams, quiet=True)
        else:
            self.ctx.log("Cameras: old settings will be put back when HOTWire can reach the Jetson again")
        self.tune_status.setText(reason)
        self._refresh_panel()

    # ------------------------------------------------------------ keep / undo
    def keep(self, cams: list[str]):
        dev = self.device()
        for cam in cams:
            if not cam or cam in self.runs or dev is None:
                continue
            if self.pending.get(dev.name, cam) is None and cam not in self.review:
                continue
            self.pending.remove(dev.name, cam)
            self.owned.discard(cam)
            self.review.pop(cam, None)
            if cam in self.tiles:
                self.tiles[cam].before_image = None
                self.tiles[cam].set_show_before(False)
            self.ctx.log(f"Cameras: {cam}: new settings kept")
        self.b_before.setChecked(False)
        self._refresh_panel()

    def undo(self, cams: list[str], quiet: bool = False):
        dev = self.device() or self._tune_dev
        if dev is None:
            return
        todo = {c: self.pending.get(dev.name, c) for c in cams if c and c not in self.runs}
        todo = {c: b for c, b in todo.items() if b}
        if not todo:
            return
        g = self.ctx.gate(manual=True, transfer=False)  # putting settings back is OK while enabled, not on the field
        if not g.allowed:
            if not quiet:
                QMessageBox.warning(self, "Not allowed now", g.reason)
            return
        s = self.ctx.settings

        def work(job):
            done, failed = [], {}
            for cam, before in todo.items():
                try:
                    http_post_json(dev, s, _settings_url(cam), before)
                    done.append(cam)
                except Exception as e:
                    failed[cam] = str(e).splitlines()[0][:120]
            return done, failed

        def finished(res):
            done, failed = res
            for cam in done:
                self.pending.remove(dev.name, cam)
                self.owned.discard(cam)
                self.review.pop(cam, None)
                if cam in self.tiles:
                    self.tiles[cam].before_image = None
                    self.tiles[cam].set_show_before(False)
                if cam == self.selected:
                    self._fill_spins(todo[cam])
                self.ctx.log(f"Cameras: {cam}: settings from before put back ({_fmt_settings(todo[cam])})")
            for cam, err in failed.items():
                self.ctx.log(f"Cameras: {cam}: couldn't put the old settings back ({err}); will offer again")
            self.b_before.setChecked(False)
            self._refresh_panel()

        Job(work).start(finished, lambda e: self.ctx.log(f"Cameras: undo failed: {e}"))

    def _recovery_check(self, _state=None):
        """A tuning run from an earlier HOTWire session (closed or crashed before Keep/Undo): offer to undo it."""
        if self._asked_recover or self.runs:
            return
        dev = self.device()
        if dev is None or not self.ctx.nt.connected:
            return
        stale = {c: b for c, b in self.pending.cameras(dev.name).items() if c not in self.owned}
        if not stale or not self.ctx.gate(manual=True, transfer=False).allowed:
            return
        self._asked_recover = True
        r = QMessageBox.question(
            self,
            "Camera tuning wasn't finished",
            f"Camera settings on {', '.join(sorted(stale))} were changed by HOTWire, but it closed before "
            "Keep or Undo.\n\nPut back the settings from before? (No = keep the current settings.)",
        )
        if r == QMessageBox.StandardButton.Yes:
            self.undo(sorted(stale), quiet=True)
        else:
            for cam in stale:
                self.pending.remove(dev.name, cam)
            self.ctx.log(f"Cameras: kept the current settings on {', '.join(sorted(stale))}")

    def shutdown(self):
        """Quitting: stop streams and tuning; put back settings that weren't kept (short, blocking)."""
        self.tune_timer.stop()
        self.runs.clear()
        if self._fast_uid is not None:
            self.ctx.nt.unsubscribe(self._fast_uid)
            self._fast_uid = None
        self.stop_all()
        dev = self.device() or self._tune_dev
        if dev is None:
            return
        cams = {c: b for c, b in self.pending.cameras(dev.name).items() if c in self.owned}
        if not cams or not self.ctx.gate(manual=True, transfer=False).allowed:
            return
        for cam, before in cams.items():
            try:
                http_post_json(dev, self.ctx.settings, _settings_url(cam), before)
                self.pending.remove(dev.name, cam)
                self.ctx.log(f"Cameras: {cam}: not kept, settings from before put back")
            except Exception as e:  # the next start offers to put them back
                self.ctx.log(f"Cameras: {cam}: couldn't put the old settings back on quit ({e})")

    # ------------------------------------------------------------ panel text
    def _show_before_toggled(self, on: bool):
        t = self.tiles.get(self.selected or "")
        if t is None:
            return
        if on and t.before_image is None:
            self.b_before.setChecked(False)
            return
        t.set_show_before(on)

    def _refresh_status(self):
        lines = [f"{c}: {r.tune.progress}" for c, r in sorted(self.runs.items())]
        dev = self.device()
        waiting = set(self.review) | (set(self.pending.cameras(dev.name)) if dev else set())
        lines += [f"{c}: changed — Keep or Undo" for c in sorted(waiting - set(self.runs))]
        self.tune_status.setText("\n".join(lines))
        busy = bool(self.runs)
        self.b_tune.setEnabled(not busy)
        self.b_tune_all.setEnabled(not busy)
        self.b_stop.setEnabled(busy)
        self.b_apply.setEnabled(not busy)

    def _refresh_panel(self):
        self._refresh_status()
        cam = self.selected
        run = self.runs.get(cam or "")
        rev = self.review.get(cam or "", {})
        if run is not None:
            before, after = run.tune.before, None
            bm, am = run.tune.before_measure, None
            bs, as_ = run.tune.before_score, None
        else:
            before, after = rev.get("before"), rev.get("after")
            bm, am = rev.get("before_m"), rev.get("after_m")
            bs, as_ = rev.get("before_score"), rev.get("after_score")
        rows: list[tuple[str, str, str]] = []
        for k in ct.TUNE_KEYS:
            rows.append((ct.LABELS[k], _num(before, k), _num(after, k)))
        for label, fn in (
            ("Tags per frame", lambda m: f"{m.tags:.2f}"),
            ("Capture fps", lambda m: "-" if m.fps is None else f"{m.fps:.0f}"),
            ("Ambiguity", lambda m: "-" if m.ambiguity is None else f"{m.ambiguity:.2f}"),
            ("Picture brightness", lambda m: "-" if m.mean is None else f"{m.mean:.0f}"),
            ("Picture contrast", lambda m: "-" if m.contrast is None else f"{m.contrast:.0f}"),
            ("Blown highlights", lambda m: "-" if m.blown is None else f"{m.blown * 100:.1f} %"),
            ("Crushed shadows", lambda m: "-" if m.crushed is None else f"{m.crushed * 100:.1f} %"),
            ("Picture", lambda m: ct.picture_problem(m) or ("OK" if m.mean is not None else "-")),
        ):
            rows.append((label, fn(bm) if bm else "", fn(am) if am else ""))
        rows.append(("Score", "" if bs is None else f"{bs:.1f}", "" if as_ is None else f"{as_:.1f}"))
        self.compare.setRowCount(len(rows))
        self.compare.setVerticalHeaderLabels([r[0] for r in rows])
        for i, (_, b, a) in enumerate(rows):
            self.compare.setItem(i, 0, QTableWidgetItem(b))
            self.compare.setItem(i, 1, QTableWidgetItem(a))
        changed = bool(cam) and cam in self._changed_cams()
        self.b_keep.setEnabled(changed)
        self.b_undo.setEnabled(changed)
        self.b_before.setEnabled(bool(cam) and cam in self.tiles and self.tiles[cam].before_image is not None)
        self.b_keep_all.setEnabled(bool(self._changed_cams()))
        self.b_undo_all.setEnabled(bool(self._changed_cams()))


def _num(d: dict | None, k: str) -> str:
    if not d or k not in d:
        return ""
    v = d[k]
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def _fmt_settings(d: dict) -> str:
    return ", ".join(f"{ct.LABELS.get(k, k).lower()} {_num(d, k)}" for k in ct.TUNE_KEYS if k in d)
