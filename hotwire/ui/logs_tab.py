"""Logs tab: list robot log files, Auto/Manual download, verify, delete."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hotwire import logsync, sshconn
from hotwire.ui.common import Job, status_color

AUTO_RECHECK_S = 60  # while allowed, look for new files this often


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class LogsTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.files: list[logsync.RemoteFile] = []
        self.job: Job | None = None
        self._last_auto = 0.0
        self._auto_pending = False

        top = QHBoxLayout()
        top.addWidget(QLabel("Device:"))
        self.dev_combo = QComboBox()
        top.addWidget(self.dev_combo)
        top.addSpacing(16)
        self.rb_auto = QRadioButton("Auto")
        self.rb_manual = QRadioButton("Manual")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_auto)
        grp.addButton(self.rb_manual)
        top.addWidget(QLabel("Mode:"))
        top.addWidget(self.rb_auto)
        top.addWidget(self.rb_manual)
        top.addSpacing(16)
        self.disk_label = QLabel("Robot disk: ?")
        top.addWidget(self.disk_label)
        top.addStretch(1)
        self.save_label = QLabel()
        top.addWidget(self.save_label)
        btn_open = QPushButton("Open save folder")
        btn_open.clicked.connect(self._open_folder)
        top.addWidget(btn_open)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "File", "Size", "Modified (robot clock)", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_download = QPushButton("Download selected")
        self.btn_delete = QPushButton("Delete selected from robot…")
        self.btn_cancel = QPushButton("Cancel")
        self.cb_delete = QCheckBox("Delete from robot after verified download")
        for b in (self.btn_refresh, self.btn_download, self.btn_delete, self.btn_cancel):
            btns.addWidget(b)
        btns.addWidget(self.cb_delete)
        btns.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("")

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(btns)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)

        self.btn_refresh.clicked.connect(lambda: self.refresh(manual=True))
        self.btn_download.clicked.connect(self.download_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_cancel.clicked.connect(self._cancel)
        self.rb_auto.toggled.connect(self._mode_changed)
        self.cb_delete.toggled.connect(self._delete_toggled)
        ctx.robot_connected.connect(self._on_robot_connected)
        ctx.settings_changed.connect(self.load_settings)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_tick)
        self.load_settings()
        self.rb_auto.setChecked(True)
        self._set_busy(False)

    # ------------------------------------------------------------ settings
    def load_settings(self):
        s = self.ctx.settings
        cur = self.dev_combo.currentText()
        self.dev_combo.clear()
        for d in s.devices:
            if d.kind in ("systemcore", "other"):
                self.dev_combo.addItem(d.name)
        if cur:
            i = self.dev_combo.findText(cur)
            if i >= 0:
                self.dev_combo.setCurrentIndex(i)
        self.cb_delete.setChecked(s.delete_after_verify)
        self.save_label.setText(f"Save to: {s.save_folder}")
        self.timer.start(max(1, s.poll_seconds) * 1000)

    def device(self):
        return self.ctx.settings.device(self.dev_combo.currentText())

    def _delete_toggled(self, on):
        self.ctx.settings.delete_after_verify = on
        self.ctx.settings.save()

    def _open_folder(self):
        p = Path(self.ctx.settings.save_folder)
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))  # noqa: S606 (Windows only)

    def _mode_changed(self, auto: bool):
        self.ctx.log(f"Logs: {'Auto' if auto else 'Manual'} mode")
        if auto:
            self._auto_pending = True

    # ------------------------------------------------------------ auto
    def _on_robot_connected(self):
        self._auto_pending = True  # tether just plugged in / robot booted

    def _auto_tick(self):
        if not self.rb_auto.isChecked() or self.job is not None:
            return
        dev = self.device()
        if dev is None or not dev.auto:
            return
        due = self._auto_pending or (time.time() - self._last_auto > AUTO_RECHECK_S)
        if not due:
            return
        g = self.ctx.gate(manual=False)
        if not g.allowed:
            self.status.setText(f"Auto: waiting ({g.reason})")
            return
        self._auto_pending = False
        self._last_auto = time.time()
        self._start_sync(dev, None, delete=self.cb_delete.isChecked(), auto=True)

    # ------------------------------------------------------------ manual
    def _manual_gate(self) -> bool:
        g = self.ctx.gate(manual=True)
        if not g.allowed:
            QMessageBox.warning(self, "Not allowed now", g.reason)
            return False
        if g.needs_confirm:
            r = QMessageBox.question(
                self,
                "Robot state unknown",
                "HOTWire can't read the robot's state (is robot code running?).\n\n"
                "Only continue if the robot is in the PITS, not on the field.\n\nContinue?",
            )
            return r == QMessageBox.StandardButton.Yes
        return True

    def refresh(self, manual=True):
        dev = self.device()
        if dev is None or self.job is not None:
            return
        if manual and not self._manual_gate():
            return
        self._set_busy(True, "Listing robot files…")

        def work(job):
            with sshconn.connect(dev, self.ctx.settings) as conn:
                fs = logsync.SSHRemoteFS(conn)
                files = logsync.list_logs(fs, dev.log_dir, dev.log_exts, self.ctx.settings.in_use_seconds)
                return files, fs.disk(dev.log_dir)

        self.job = Job(work).start(self._listed, self._failed)

    def _listed(self, res):
        files, disk = res
        self._set_busy(False)
        self._show_files(files, disk)
        self.status.setText(f"{len(files)} log files on robot")

    def selected_files(self) -> list[logsync.RemoteFile]:
        out = []
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 0)
            if it and it.checkState() == Qt.CheckState.Checked:
                out.append(self.files[row])
        return out

    def download_selected(self):
        dev = self.device()
        sel = self.selected_files()
        if dev is None or not sel or self.job is not None:
            if not sel:
                QMessageBox.information(self, "Nothing selected", "Tick the files to download first.")
            return
        if not self._manual_gate():
            return
        self._start_sync(dev, sel, delete=self.cb_delete.isChecked(), auto=False)

    def delete_selected(self):
        dev = self.device()
        sel = [f for f in self.selected_files() if not f.in_use]
        if dev is None or not sel or self.job is not None:
            return
        unsaved = [f for f in sel if not self.ctx.index.get(dev.name, f)]
        msg = f"Permanently delete {len(sel)} file(s) from {dev.name}?"
        if unsaved:
            msg += f"\n\n⚠ {len(unsaved)} of them have NOT been downloaded to this laptop."
        if QMessageBox.question(self, "Delete from robot", msg) != QMessageBox.StandardButton.Yes:
            return
        if not self._manual_gate():
            return
        self._set_busy(True, "Deleting…")

        def work(job):
            n = 0
            with sshconn.connect(dev, self.ctx.settings) as conn:
                fs = logsync.SSHRemoteFS(conn)
                for f in sel:
                    fs.remove(f.path)
                    self.ctx.log(f"Deleted from {dev.name}: {f.rel}")
                    n += 1
                files = logsync.list_logs(fs, dev.log_dir, dev.log_exts, self.ctx.settings.in_use_seconds)
                return n, files, fs.disk(dev.log_dir)

        def done(res):
            n, files, disk = res
            self._set_busy(False)
            self._show_files(files, disk)
            self.status.setText(f"Deleted {n} file(s)")

        self.job = Job(work).start(done, self._failed)

    # ------------------------------------------------------------ sync
    def _start_sync(self, dev, only: list | None, delete: bool, auto: bool):
        s = self.ctx.settings
        ctx = self.ctx
        self._set_busy(True, "Connecting…")

        def still_allowed():
            return ctx.gate(manual=not auto).allowed

        def work(job):
            results = []
            with sshconn.connect(dev, s) as conn:
                fs = logsync.SSHRemoteFS(conn)
                files = logsync.list_logs(fs, dev.log_dir, dev.log_exts, s.in_use_seconds)
                job.progress.emit(("files", files, fs.disk(dev.log_dir)))
                if only is not None:
                    keys = {f.rel for f in only}
                    targets = [f for f in files if f.rel in keys]
                else:
                    targets = [f for f in files if not f.in_use and (delete or not ctx.index.get(dev.name, f))]
                    if s.auto_match_logs_only:
                        targets = [f for f in targets if logsync.is_match_log(f)]
                for i, f in enumerate(targets):
                    if job.cancel.is_set():
                        break
                    if not still_allowed():
                        ctx.log("Stopped: robot state changed (enabled / on field / lock)")
                        break

                    def prog(done, total, i=i, f=f):
                        if not still_allowed():
                            job.cancel.set()
                        job.progress.emit(("bytes", i, len(targets), f.name, done, total))

                    try:
                        r = logsync.sync_file(
                            fs, dev.name, f, s.save_folder, ctx.index, delete, s.speed_limit_kbps, prog, job.cancel
                        )
                    except logsync.Cancelled:
                        ctx.log(f"Cancelled: {f.rel}")
                        break
                    results.append(r)
                    ctx.log(f"{dev.name}: {f.rel}: {r.message}")
                files = logsync.list_logs(fs, dev.log_dir, dev.log_exts, s.in_use_seconds)
                return results, files, fs.disk(dev.log_dir)

        def on_progress(p):
            if p[0] == "files":
                self._show_files(p[1], p[2])
            else:
                _, i, n, name, done, total = p
                self.progress.setMaximum(max(total, 1))
                self.progress.setValue(done)
                self.status.setText(f"[{i + 1}/{n}] {name}  {_size(done)} / {_size(total)}")

        def on_done(res):
            results, files, disk = res
            self._set_busy(False)
            self._show_files(files, disk)
            ok = [r for r in results if r.ok]
            bad = [r for r in results if not r.ok and "in use" not in r.message]
            if results:
                size = sum(r.rf.size for r in ok)
                msg = f"Pulled {len(ok)} file(s), {_size(size)}" + (f"; {len(bad)} problem(s)" if bad else " ✓")
                self.status.setText(msg)
                ctx.notify.emit("HOTWire", msg)
            else:
                self.status.setText(f"Up to date ({datetime.now():%H:%M:%S})")

        def on_fail(err):
            self._failed(err, quiet=auto)

        self.job = Job(work).start(on_done, on_fail, on_progress)

    # ------------------------------------------------------------ ui helpers
    def _failed(self, err, quiet=False):
        self._set_busy(False)
        self.status.setText(err.splitlines()[0][:200])
        self.ctx.log(f"Logs: {err.splitlines()[0]}")
        if not quiet:
            QMessageBox.warning(self, "HOTWire", err)

    def _cancel(self):
        if self.job:
            self.job.cancel.set()

    def _set_busy(self, busy: bool, text: str = ""):
        if not busy:
            self.job = None
        for b in (self.btn_refresh, self.btn_download, self.btn_delete):
            b.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.progress.setVisible(busy)
        self.progress.setValue(0)
        if text:
            self.status.setText(text)

    def _show_files(self, files, disk):
        dev = self.device()
        checked = {f.rel for f in self.selected_files()} if self.files else set()
        self.files = files
        if disk:
            used, total = disk
            pct = 100 * used / total if total else 0
            self.disk_label.setText(f"Robot disk: {_size(used)} / {_size(total)} ({pct:.0f}%)")
            self.disk_label.setStyleSheet("color:#c73636; font-weight:600;" if pct > 85 else "")
        self.table.setRowCount(len(files))
        for row, f in enumerate(files):
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            cb.setCheckState(Qt.CheckState.Checked if f.rel in checked else Qt.CheckState.Unchecked)
            if f.in_use:
                cb.setFlags(Qt.ItemFlag.NoItemFlags)
            self.table.setItem(row, 0, cb)
            self.table.setItem(row, 1, QTableWidgetItem(f.rel))
            self.table.setItem(row, 2, QTableWidgetItem(_size(f.size)))
            self.table.setItem(row, 3, QTableWidgetItem(datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")))
            if f.in_use:
                st, color = "in use 🔒", "pending"
            elif dev and self.ctx.index.get(dev.name, f):
                st, color = "✓ saved", "pass"
            elif logsync.is_match_log(f):
                st, color = "new match log", "running"
            else:
                st, color = "practice (manual)" if self.ctx.settings.auto_match_logs_only else "new", "info"
            item = QTableWidgetItem(st)
            item.setForeground(status_color(color))
            self.table.setItem(row, 4, item)
        self.table.resizeColumnToContents(0)
