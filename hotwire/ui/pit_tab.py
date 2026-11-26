"""Pit Test tab: record NT signals live, check them, analyze logs, learn limits."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hotwire.pitcheck import analyze as an
from hotwire.pitcheck import config as pc
from hotwire.pitcheck.recorder import Recorder, load_csv, save_csv
from hotwire.pitcheck.wpilog import read_wpilog
from hotwire.ui.common import status_color

# In the .exe (PyInstaller) bundled files live under sys._MEIPASS
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
EXAMPLE = _BASE / "examples" / "pit_checks.yaml"
LIVE_GRACE_S = 0.5  # live: grade a check this long after its window ends (NT data arrives in batches)
NUMERIC_TYPES = {"double", "float", "int", "boolean"}


class PitTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.cfg: pc.PitConfig | None = None
        self.recorder: Recorder | None = None
        self.rec: an.Recording = {}
        self.results: list[an.CheckResult] = []
        self.rec_source = ""

        # --- config row
        cfg_row = QHBoxLayout()
        self.cfg_label = QLabel()
        self.cfg_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cfg_row.addWidget(self.cfg_label, 1)
        for text, fn in (("Edit checks…", self.edit_config), ("Reload", self.load_config)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            cfg_row.addWidget(b)
        self.errors = QLabel()
        self.errors.setWordWrap(True)
        self.errors.setStyleSheet("color:#c73636;")

        # --- action row
        act = QHBoxLayout()
        self.btn_rec = QPushButton("●  Start recording")
        self.btn_rec.setStyleSheet("font-weight:600; padding:6px 14px;")
        self.btn_rec.clicked.connect(self.toggle_recording)
        self.btn_file = QPushButton("Analyze a log file…")
        self.btn_file.clicked.connect(self.analyze_file)
        self.btn_learn = QPushButton("Learn limits from this run…")
        self.btn_learn.clicked.connect(self.learn)
        for b in (self.btn_rec, self.btn_file, self.btn_learn):
            act.addWidget(b)
        act.addStretch(1)
        self.summary = QLabel("")
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        self.summary.setFont(f)
        act.addWidget(self.summary)
        self.rec_label = QLabel("")

        # --- the robot's current pit step (2027 robot code publishes HOT/Pit/Step), live
        self.step_label = QLabel("")
        fs = QFont()
        fs.setPointSize(14)
        fs.setBold(True)
        self.step_label.setFont(fs)
        self.step_label.setStyleSheet("padding:4px 8px; background:#eef2f7; border-radius:4px;")
        self.step_label.setToolTip("The step the robot's Pit Test opmode is on (NT topic set by step_signal)")
        self._step_uid = None
        self.step_timer = QTimer(self)
        self.step_timer.timeout.connect(self._show_step)
        ctx.tick.connect(self._step_sync)  # subscribe while visible or recording; never with the FMS

        # --- results + chart
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Check / signal", "Status", "Value", "Limit", "Note"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(lambda *_: self.plot_selected())
        self.chart = QChart()
        self.chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.chart_view)
        split.setSizes([300, 260])

        lay = QVBoxLayout(self)
        lay.addLayout(cfg_row)
        lay.addWidget(self.errors)
        lay.addLayout(act)
        lay.addWidget(self.step_label)
        lay.addWidget(self.rec_label)
        lay.addWidget(split, 1)

        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._live_update)
        ctx.settings_changed.connect(self.load_config)
        self.load_config()

    # ------------------------------------------------------------ config
    def cfg_path(self) -> Path:
        return self.ctx.settings.pit_config_path()

    def load_config(self):
        p = self.cfg_path()
        if not p.exists():
            self.cfg = None
            self.cfg_label.setText(f"Checks file: {p}  (not found)")
            self.errors.setText("No checks file yet. Click 'Edit checks…' to start from the example.")
            return
        self.cfg = pc.load(p)
        self.cfg_label.setText(f"Checks file: {p}  ({len(self.cfg.checks)} checks, {len(self.cfg.signals)} signals)")
        self.errors.setText("\n".join(self.cfg.errors[:12]))
        if self.rec:
            self.run_analysis(final=self.recorder is None)

    def edit_config(self):
        p = self.cfg_path()
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            if EXAMPLE.exists():
                shutil.copy(EXAMPLE, p)
            else:
                p.write_text("signals: {}\nchecks: []\n", encoding="utf-8")
        dlg = ConfigEditor(self.ctx, p, self)
        if dlg.exec():
            self.load_config()

    # ------------------------------------------------------------ pit step (live)
    def step_topic(self) -> str:
        return self.cfg.step_topic if self.cfg else pc.DEFAULT_STEP_TOPIC

    def _step_sync(self, state=None):
        st = state or self.ctx.state
        want = (self.isVisible() or self.recorder is not None) and not st.fms_attached and not self.ctx.locked
        if want and self._step_uid is None:
            self._step_uid = self.ctx.nt.subscribe([self.step_topic()], periodic=0.1)
            self.step_timer.start(200)
        elif not want:
            self._step_unsubscribe()
        self._show_step()

    def _step_unsubscribe(self):
        if self._step_uid is not None:
            self.ctx.nt.unsubscribe(self._step_uid)
            self._step_uid = None
        self.step_timer.stop()

    def current_step(self) -> str:
        if self._step_uid is None:
            return ""
        v = self.ctx.nt.get(self.step_topic())
        return v if isinstance(v, str) else ""

    def _show_step(self):
        step = self.current_step()
        if step:
            self.step_label.setText(f"Robot pit step:  {step}")
        elif self._step_uid is not None and self.ctx.nt.connected:
            self.step_label.setText(f"Robot pit step:  (not published: {self.step_topic()})")
        else:
            self.step_label.setText("Robot pit step:  (robot not connected)")

    def showEvent(self, e):
        super().showEvent(e)
        self._step_sync()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._step_sync()

    def shutdown(self):
        if self.recorder is not None:
            self.stop_recording()
        self._step_unsubscribe()

    # ------------------------------------------------------------ recording
    def toggle_recording(self):
        if self.recorder is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        if self.cfg is None or not self.cfg.signals:
            QMessageBox.warning(self, "No checks", "Set up the checks file first (Edit checks…).")
            return
        g = self.ctx.gate(manual=True, transfer=False)
        if not g.allowed:
            QMessageBox.warning(self, "Not allowed now", g.reason)
            return
        if not self.ctx.nt.connected:
            QMessageBox.warning(self, "Robot not connected", "NetworkTables isn't connected to the robot.")
            return
        # the step name is always recorded with the run (saved in recording.csv)
        self.recorder = Recorder(self.ctx.nt, sorted(set(self.cfg.all_topics()) | {self.step_topic()}))
        self.recorder.start()
        self.rec_source = "live"
        self.btn_rec.setText("■  Stop && analyze")
        self.btn_file.setEnabled(False)
        self.btn_learn.setEnabled(False)
        self.ctx.log("Pit test: recording started")
        self.live_timer.start(300)

    def stop_recording(self):
        if self.recorder is None:
            return
        self.live_timer.stop()
        self.recorder.stop()
        self.rec = self.recorder.snapshot()
        self.recorder = None
        self.btn_rec.setText("●  Start recording")
        self.btn_file.setEnabled(True)
        self.btn_learn.setEnabled(True)
        self.run_analysis(final=True)
        folder = self.save_run()
        self.ctx.log(f"Pit test: {an.summary(self.results)}; saved to {folder}")
        self.ctx.notify.emit("HOTWire pit test", an.summary(self.results))

    def _live_update(self):
        if self.recorder is None:
            return
        if not self.ctx.gate(manual=True, transfer=False).allowed:
            self.ctx.log("Pit test: stopped (robot on field / lock)")
            self.stop_recording()
            return
        self.rec = self.recorder.snapshot()
        n = sum(len(v) for v in self.rec.values())
        t = self.recorder.now() - self.recorder.t_start_us / 1e6
        silent = [k for k, v in self.rec.items() if not v and k != self.step_topic()]
        self.rec_label.setText(
            f"Recording {t:5.1f} s · {n} samples"
            + (f" · no data yet from {len(silent)} signal(s): {', '.join(silent[:3])}" if silent else "")
        )
        self.run_analysis(final=False, data_end=self.recorder.now())

    def save_run(self) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder = Path(self.ctx.settings.save_folder) / "PitTests" / stamp
        folder.mkdir(parents=True, exist_ok=True)
        save_csv(self.rec, folder / "recording.csv")
        (folder / "report.txt").write_text(an.report_text(self.results), encoding="utf-8")
        steps = self.rec.get(self.step_topic(), [])
        report = [
            {
                "check": r.check.name,
                "robot_step": an.step_at(steps, r.start) if r.start is not None else "",
                "status": r.status,
                "start": r.start,
                "end": r.end,
                "note": r.note,
                "metrics": [asdict(m) for m in r.metrics],
            }
            for r in self.results
        ]
        (folder / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        if self.cfg and self.cfg.path and self.cfg.path.exists():
            shutil.copy(self.cfg.path, folder / "pit_checks.yaml")  # which limits were used
        return folder

    # ------------------------------------------------------------ files
    def analyze_file(self):
        if self.cfg is None:
            QMessageBox.warning(self, "No checks", "Set up the checks file first.")
            return
        start = str(Path(self.ctx.settings.save_folder))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a robot log or HOTWire recording", start, "Logs (*.wpilog recording.csv *.csv)"
        )
        if not path:
            return
        try:
            if path.lower().endswith(".wpilog"):
                rec, _types = read_wpilog(path, self.cfg.all_topics(), string_topics=[self.step_topic()])
            else:
                rec = load_csv(Path(path))
        except Exception as e:
            QMessageBox.warning(self, "Can't read file", str(e))
            return
        missing = [t for t in self.cfg.all_topics() if not rec.get(t) and t != self.step_topic()]
        self.rec = rec
        self.rec_source = path
        self.run_analysis(final=True)
        self.rec_label.setText(
            f"Analyzed {Path(path).name}"
            + (f" · {len(missing)} signal(s) not in this file: {', '.join(missing[:3])}" if missing else "")
        )

    # ------------------------------------------------------------ analysis
    def run_analysis(self, final: bool, data_end: float | None = None):
        if self.cfg is None:
            return
        self.results = an.analyze(self.cfg, self.rec, data_end=data_end, final=final, grace=LIVE_GRACE_S)
        self._fill_tree()
        overall = an.worst(r.status if r.status != "missing" else "fail" for r in self.results) if final else ""
        txt = an.summary(self.results)
        self.summary.setText((f"{overall.upper()}  ·  " if overall else "") + txt)
        self.summary.setStyleSheet(f"color:{status_color(overall or 'running').name()};")

    def _fill_tree(self):
        cur = self.tree.currentItem()
        cur_key = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self.tree.clear()
        for i, r in enumerate(self.results):
            top = QTreeWidgetItem([r.check.name, r.status, "", "", r.note])
            top.setData(0, Qt.ItemDataRole.UserRole, (i, None))
            top.setForeground(1, status_color(r.status))
            f = top.font(1)
            f.setBold(True)
            top.setFont(1, f)
            for m in r.metrics:
                v = "-" if m.value is None else f"{m.value:.3g}"
                ch = QTreeWidgetItem([f"{m.signal} · {m.kind}", m.status, v, m.limit, m.detail])
                ch.setData(0, Qt.ItemDataRole.UserRole, (i, m.signal))
                ch.setForeground(1, status_color(m.status))
                top.addChild(ch)
            self.tree.addTopLevelItem(top)
            top.setExpanded(r.status in ("fail", "warn", "running"))
            if cur_key and cur_key[0] == i and cur_key[1] is None:
                self.tree.setCurrentItem(top)

    def plot_selected(self):
        self.chart.removeAllSeries()
        for ax in self.chart.axes():
            self.chart.removeAxis(ax)
        it = self.tree.currentItem()
        if it is None or self.cfg is None:
            return
        i, sig = it.data(0, Qt.ItemDataRole.UserRole)
        r = self.results[i]
        if r.start is None:
            self.chart.setTitle(f"{r.check.name}: not seen")
            return
        t0, t1 = r.start - 0.5, (r.end or r.start) + 0.5
        aliases = [sig] if sig else list(r.check.expect.keys())
        ax_x, ax_y = QValueAxis(), QValueAxis()
        ax_x.setTitleText("seconds from start")
        self.chart.addAxis(ax_x, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(ax_y, Qt.AlignmentFlag.AlignLeft)
        ymin, ymax = float("inf"), float("-inf")

        def add(series):
            self.chart.addSeries(series)
            series.attachAxis(ax_x)
            series.attachAxis(ax_y)

        for alias in aliases:
            vals = an.resample(self.rec.get(self.cfg.topic(alias), []), t0, t1)
            if vals.size == 0:
                continue
            s = QLineSeries()
            s.setName(alias)
            step = max(1, vals.size // 1500)
            # x = seconds since the check started (resample began 0.5 s earlier)
            s.append([QPointF(k * an.DT - 0.5, float(vals[k])) for k in range(0, vals.size, step)])
            add(s)
            ymin, ymax = min(ymin, float(vals.min())), max(ymax, float(vals.max()))
            spec = r.check.expect.get(alias, {})
            lines = []
            for key in ("peak_max", "max"):
                if key in spec:
                    lines.append((f"{alias} {key}", spec[key], "#c73636"))
            for key in ("peak_min", "min"):
                if key in spec:
                    lines.append((f"{alias} {key}", spec[key], "#d08b00"))
            if "steady" in spec:
                lines += [(f"{alias} steady lo", spec["steady"][0], "#2e9d4f"), (f"{alias} steady hi", spec["steady"][1], "#2e9d4f")]
            tgt = spec.get("settle_to", spec.get("final"))
            if tgt is not None:
                lines.append((f"{alias} target", tgt, "#2f74c0"))
            for name, y, color in lines:
                ls = QLineSeries()
                ls.setName(name)
                ls.append(-0.5, y)
                ls.append(t1 - t0 - 0.5, y)
                pen = QPen(QColor(color))
                pen.setStyle(Qt.PenStyle.DashLine)
                ls.setPen(pen)
                add(ls)
                ymin, ymax = min(ymin, y), max(ymax, y)
        if ymin == float("inf"):
            ymin, ymax = 0, 1
        pad = (ymax - ymin) * 0.08 or 1
        ax_y.setRange(ymin - pad, ymax + pad)
        ax_x.setRange(-0.5, t1 - t0 - 0.5)
        self.chart.setTitle(f"{r.check.name}  ({r.status})")

    # ------------------------------------------------------------ learn
    def learn(self):
        if self.cfg is None or not self.rec:
            QMessageBox.information(self, "No run", "Record a known-good pit test (or open a log) first.")
            return
        sugg = an.learn(self.cfg, self.rec)
        if not sugg:
            QMessageBox.information(self, "Nothing to learn", "None of the checks were seen in this run.")
            return
        dlg = LearnDialog(sugg, self)
        if not dlg.exec():
            return
        chosen = dlg.chosen()
        if not chosen:
            return
        p = self.cfg_path()
        text = p.read_text(encoding="utf-8")
        shutil.copy(p, p.with_suffix(p.suffix + ".bak"))
        p.write_text(an.apply_suggestions(text, chosen), encoding="utf-8")
        self.ctx.log(f"Pit test: applied {len(chosen)} learned limit(s) to {p.name} (backup: {p.name}.bak)")
        self.load_config()


class LearnDialog(QDialog):
    def __init__(self, sugg: list[an.Suggestion], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Learn limits from this run")
        self.resize(900, 500)
        self.sugg = sugg
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "Only use this on a run where the robot worked correctly. Targets (final / settle_to) are never changed.\n"
                "Untick anything you don't want. The old file is kept as .bak."
            )
        )
        self.table = QTableWidget(len(sugg), 6)
        self.table.setHorizontalHeaderLabels(["Use", "Check", "Signal", "Key", "Current → Suggested", "Observed"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for row, s in enumerate(sugg):
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            cb.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, cb)
            for col, val in enumerate([s.check_name, s.signal, s.key, f"{s.old} → {s.new}", s.observed], start=1):
                self.table.setItem(row, col, QTableWidgetItem(str(val)))
        lay.addWidget(self.table)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply ticked")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self):
        return [s for row, s in enumerate(self.sugg) if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]


class ConfigEditor(QDialog):
    """Plain-text YAML editor with signal picker and validation."""

    def __init__(self, ctx, path: Path, parent=None):
        super().__init__(parent)
        self.ctx, self.path = ctx, path
        self.setWindowTitle(f"Pit checks - {path}")
        self.resize(1000, 720)
        self.edit = QPlainTextEdit()
        f = QFont("Consolas", 10)
        self.edit.setFont(f)
        self.edit.setPlainText(path.read_text(encoding="utf-8"))
        self.errors = QListWidget()
        self.errors.setMaximumHeight(130)

        row = QHBoxLayout()
        row.addWidget(QLabel("Insert robot signal:"))
        self.sig_combo = QComboBox()
        self.sig_combo.setEditable(True)
        self.sig_combo.setMinimumWidth(480)
        row.addWidget(self.sig_combo, 1)
        b_ins = QPushButton("Insert at cursor")
        b_ins.clicked.connect(self._insert)
        row.addWidget(b_ins)
        b_ref = QPushButton("Refresh list")
        b_ref.clicked.connect(self._fill_signals)
        row.addWidget(b_ref)
        self.cb_alias = QCheckBox("as 'alias: topic' line")
        self.cb_alias.setChecked(True)
        row.addWidget(self.cb_alias)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        b_val = bb.addButton("Check for errors", QDialogButtonBox.ButtonRole.ActionRole)
        b_val.clicked.connect(self.validate)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.edit, 1)
        lay.addWidget(QLabel("Problems:"))
        lay.addWidget(self.errors)
        lay.addWidget(bb)
        # topic names only (no values): NT4 announces only subscribed topics
        self._uid = ctx.nt.subscribe(["/"], prefix=True, topics_only=True)
        self.finished.connect(lambda _: ctx.nt.unsubscribe(self._uid))
        QTimer.singleShot(700, self._fill_signals)
        self._fill_signals()
        self.validate()

    def _fill_signals(self):
        self.sig_combo.clear()
        topics = [t.name for t in self.ctx.nt.topics() if t.type in NUMERIC_TYPES]
        if not topics:
            self.sig_combo.setPlaceholderText("(robot not connected: no live signal list)")
        self.sig_combo.addItems(topics)

    def _insert(self):
        topic = self.sig_combo.currentText().strip()
        if not topic:
            return
        if self.cb_alias.isChecked():
            alias = topic.strip("/").split("/")[-1].replace(" ", "_").lower()
            alias = "".join(c for c in alias if c.isalnum() or c in "_.") or "signal"
            self.edit.insertPlainText(f"{alias}: {topic}\n")
        else:
            self.edit.insertPlainText(topic)

    def validate(self) -> bool:
        cfg = pc.load_text(self.edit.toPlainText())
        self.errors.clear()
        if cfg.errors:
            self.errors.addItems(cfg.errors)
            return False
        known = {t.name for t in self.ctx.nt.topics()}
        missing = [f"Robot isn't publishing {t} right now" for t in cfg.all_topics() if known and t not in known]
        self.errors.addItems(missing or [f"OK: {len(cfg.checks)} checks, {len(cfg.signals)} signals"])
        return True

    def _save(self):
        if not self.validate():
            r = QMessageBox.question(self, "Problems found", "The file has problems. Save anyway?")
            if r != QMessageBox.StandardButton.Yes:
                return
        if self.path.exists():
            shutil.copy(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        self.path.write_text(self.edit.toPlainText(), encoding="utf-8")
        self.accept()
