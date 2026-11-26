"""Settings tab: folders, safety options, and the device list with key setup."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hotwire import sshconn
from hotwire.settings import Device, HostEntry
from hotwire.ui.common import Job


def _path_row(edit: QLineEdit, parent, folder: bool, filt: str = "") -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    b = QPushButton("Browse…")

    def pick():
        if folder:
            p = QFileDialog.getExistingDirectory(parent, "Choose folder", edit.text())
        else:
            p, _ = QFileDialog.getOpenFileName(parent, "Choose file", edit.text(), filt)
        if p:
            edit.setText(p)

    b.clicked.connect(pick)
    row.addWidget(b)
    return row


class DeviceDialog(QDialog):
    def __init__(self, dev: Device | None, names: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Device")
        d = dev or Device(name="", kind="systemcore", user="", hosts=[])
        self.name = QLineEdit(d.name)
        self.kind = QComboBox()
        self.kind.addItems(["systemcore", "jetson", "other"])
        self.kind.setCurrentText(d.kind)
        self.user = QLineEdit(d.user)
        self.hosts = QPlainTextEdit("\n".join(h.host + (f" via {h.via}" if h.via else "") for h in d.hosts))
        self.hosts.setMaximumHeight(80)
        self.auto = QCheckBox("Include in Auto mode")
        self.auto.setChecked(d.auto)
        self.log_dir = QLineEdit(d.log_dir)
        self.exts = QLineEdit(", ".join(d.log_exts))
        self.hv_dir = QLineEdit(d.hotvision_dir)
        self.service = QLineEdit(d.service)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(d.web_port)
        f = QFormLayout(self)
        f.addRow("Name:", self.name)
        f.addRow("Type:", self.kind)
        f.addRow("SSH user:", self.user)
        f.addRow(
            "Addresses (one per line,\ntried in order):",
            self.hosts,
        )
        f.addRow("", QLabel(f"e.g. 10.0.67.7   or   10.0.67.7 via SystemCore   (devices: {', '.join(names)})"))
        f.addRow("", self.auto)
        f.addRow("Log folder (SystemCore):", self.log_dir)
        f.addRow("Log file types:", self.exts)
        f.addRow("HOTVision folder (Jetson):", self.hv_dir)
        f.addRow("Service (Jetson):", self.service)
        f.addRow("Web port (Jetson):", self.port)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def device(self) -> Device:
        hosts = []
        for line in self.hosts.toPlainText().splitlines():
            parts = line.split()
            if not parts:
                continue
            via = parts[2] if len(parts) >= 3 and parts[1].lower() == "via" else ""
            hosts.append(HostEntry(parts[0], via))
        return Device(
            name=self.name.text().strip(),
            kind=self.kind.currentText(),
            user=self.user.text().strip(),
            hosts=hosts,
            auto=self.auto.isChecked(),
            log_dir=self.log_dir.text().strip(),
            log_exts=[e.strip() for e in self.exts.text().split(",") if e.strip()],
            hotvision_dir=self.hv_dir.text().strip(),
            service=self.service.text().strip(),
            web_port=self.port.value(),
        )


class SettingsTab(QWidget):
    # True while the form has changes that aren't saved yet (MainWindow marks the tab).
    dirty_changed = Signal(bool)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        s = ctx.settings

        # Two label + field pairs per row (and the checkboxes in a grid), so the
        # whole tab fits on a laptop screen without scrolling.
        g = QGroupBox("General")
        grid = QGridLayout(g)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(12)

        def pair(row: int, col: int, label: str, field) -> None:
            grid.addWidget(QLabel(label), row, col * 2)
            if isinstance(field, QHBoxLayout):
                grid.addLayout(field, row, col * 2 + 1)
            else:
                grid.addWidget(field, row, col * 2 + 1)

        self.save_folder = QLineEdit(s.save_folder)
        self.save_folder.setToolTip("Tip: pick a folder inside the team's Google Drive (Drive for Desktop) to share logs.")
        pair(0, 0, "Save logs to:", _path_row(self.save_folder, self, folder=True))
        self.pit_config = QLineEdit(s.pit_config)
        self.pit_config.setPlaceholderText("(default: <save folder>/pit_checks.yaml)")
        pair(0, 1, "Pit checks file:", _path_row(self.pit_config, self, folder=False, filt="YAML (*.yaml *.yml)"))

        self.poll = QSpinBox()
        self.poll.setRange(1, 600)
        self.poll.setValue(s.poll_seconds)
        self.poll.setSuffix(" s")
        pair(1, 0, "Check every:", self.poll)
        self.speed = QSpinBox()
        self.speed.setRange(0, 1_000_000)
        self.speed.setValue(s.speed_limit_kbps)
        self.speed.setSuffix(" KB/s  (0 = no limit)")
        pair(1, 1, "Download speed limit:", self.speed)

        self.in_use = QSpinBox()
        self.in_use.setRange(5, 3600)
        self.in_use.setValue(s.in_use_seconds)
        self.in_use.setSuffix(" s")
        pair(2, 0, "File 'in use' if modified within:", self.in_use)
        self.sharp = QDoubleSpinBox()
        self.sharp.setRange(0, 100000)
        self.sharp.setValue(s.stream_sharpness_min)
        self.sharp.setSuffix("  (0 = don't judge)")
        pair(2, 1, "Camera sharpness minimum:", self.sharp)

        self.nt_dev = QComboBox()
        pair(3, 0, "NetworkTables server device:", self.nt_dev)
        self.nt_backend = QComboBox()
        self.nt_backend.addItem("Auto (pyntcore if it loads, else built-in)", "auto")
        self.nt_backend.addItem("pyntcore (official WPILib library)", "pyntcore")
        self.nt_backend.addItem("Built-in (pure Python, works with Smart App Control)", "builtin")
        self.nt_backend.setCurrentIndex(max(0, self.nt_backend.findData(s.nt_backend)))
        pair(3, 1, "NetworkTables client:", self.nt_backend)
        grid.addWidget(QLabel(f"NT client in use now: {ctx.nt_backend_desc}  (changes apply after a restart)"), 4, 3)

        self.batt_topic = QLineEdit(s.battery_topic)
        self.batt_topic.setPlaceholderText("(empty = don't show)")
        pair(5, 0, "Battery voltage NT topic:", self.batt_topic)
        self.key_path = QLineEdit(s.key_path)
        pair(5, 1, "SSH key file:", _path_row(self.key_path, self, folder=False))

        self.del_after = QCheckBox("Delete robot files after a verified download")
        self.del_after.setChecked(s.delete_after_verify)
        self.match_only = QCheckBox("Auto mode pulls only FMS match logs (practice logs: use Manual)")
        self.match_only.setChecked(s.auto_match_logs_only)
        self.start_min = QCheckBox("Start minimized (to the taskbar)")
        self.start_min.setChecked(s.start_minimized)
        self.snap_on_connect = QCheckBox("Take a Jetson snapshot automatically when the robot connects")
        self.snap_on_connect.setChecked(s.jetson_snapshot_on_connect)
        self.copy_ds = QCheckBox("Copy Driver Station logs next to the robot logs (Auto mode)")
        self.copy_ds.setChecked(s.copy_ds_logs)
        checks = QGridLayout()
        for i, cb in enumerate((self.del_after, self.match_only, self.start_min, self.snap_on_connect, self.copy_ds)):
            checks.addWidget(cb, i // 2, i % 2)
        grid.addLayout(checks, 6, 0, 1, 4)

        self.ds_dir = QLineEdit(s.ds_log_dir)
        self.ds_dir.setToolTip("Where the 2027 Driver Station writes its FIRST_DS_*.wpilog files")
        pair(7, 0, "Driver Station log folder:", _path_row(self.ds_dir, self, folder=True))
        self.ds_days = QSpinBox()
        self.ds_days.setRange(1, 365)
        self.ds_days.setValue(s.ds_log_days)
        self.ds_days.setSuffix(" days")
        pair(7, 1, "Copy DS logs from the last:", self.ds_days)

        d = QGroupBox("Devices")
        dl = QVBoxLayout(d)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "User", "Addresses", "Auto"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit_device)
        dl.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (
            ("Add", self.add_device),
            ("Edit", self.edit_device),
            ("Remove", self.remove_device),
            ("Test connection", self.test_device),
            ("Set up key (password once)…", self.setup_key),
            ("Forget host key", self.forget_host),
            ("Show laptop public key", self.show_pubkey),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        dl.addLayout(row)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        b_save = QPushButton("Save settings")
        b_save.setStyleSheet("font-weight:600; padding:6px 16px;")
        b_save.clicked.connect(self.save)
        bottom.addWidget(b_save)

        # Red bar while something is changed but not saved: changes only apply after Save.
        self.unsaved = QFrame()
        self.unsaved.setStyleSheet("QFrame { background:#c62828; border-radius:4px; } QLabel { color:white; }")
        ub = QHBoxLayout(self.unsaved)
        ub.setContentsMargins(10, 4, 10, 4)
        ub.addWidget(QLabel("<b>Unsaved settings.</b> They don't apply until you save."), 1)
        b_save2 = QPushButton("Save")
        b_save2.clicked.connect(lambda: self.save())
        b_undo = QPushButton("Undo changes")
        b_undo.clicked.connect(self.undo)
        ub.addWidget(b_save2)
        ub.addWidget(b_undo)
        self.unsaved.hide()

        lay = QVBoxLayout(self)
        lay.addWidget(g)
        lay.addWidget(d, 1)
        lay.addWidget(self.unsaved)
        lay.addLayout(bottom)
        self.devices = [Device.from_dict(_asdict(x)) for x in s.devices]
        self._fill_devices()

        self._dirty = False
        self._saved_state = self._form_state()
        for w in (self.save_folder, self.pit_config, self.batt_topic, self.key_path, self.ds_dir):
            w.textChanged.connect(self._check_dirty)
        for w in (self.poll, self.speed, self.in_use, self.sharp, self.ds_days):
            w.valueChanged.connect(self._check_dirty)
        for w in (self.nt_dev, self.nt_backend):
            w.currentIndexChanged.connect(self._check_dirty)
        for w in (self.del_after, self.match_only, self.start_min, self.snap_on_connect, self.copy_ds):
            w.toggled.connect(self._check_dirty)

    # ------------------------------------------------------------ unsaved changes
    def _form_state(self) -> tuple:
        return (
            self.save_folder.text().strip(),
            self.pit_config.text().strip(),
            self.poll.value(),
            self.speed.value(),
            self.in_use.value(),
            self.sharp.value(),
            self.nt_dev.currentText(),
            self.nt_backend.currentData(),
            self.batt_topic.text().strip(),
            self.key_path.text().strip(),
            self.del_after.isChecked(),
            self.match_only.isChecked(),
            self.start_min.isChecked(),
            self.snap_on_connect.isChecked(),
            self.copy_ds.isChecked(),
            self.ds_dir.text().strip(),
            self.ds_days.value(),
            repr([_asdict(x) for x in self.devices]),
        )

    def is_dirty(self) -> bool:
        return self._dirty

    def _check_dirty(self, *_):
        if not hasattr(self, "_saved_state"):
            return  # still building the form
        dirty = self._form_state() != self._saved_state
        if dirty != self._dirty:
            self._dirty = dirty
            self.unsaved.setVisible(dirty)
            self.dirty_changed.emit(dirty)

    def undo(self):
        """Puts the form back to the saved settings."""
        s = self.ctx.settings
        self.save_folder.setText(s.save_folder)
        self.pit_config.setText(s.pit_config)
        self.poll.setValue(s.poll_seconds)
        self.speed.setValue(s.speed_limit_kbps)
        self.in_use.setValue(s.in_use_seconds)
        self.sharp.setValue(s.stream_sharpness_min)
        self.nt_backend.setCurrentIndex(max(0, self.nt_backend.findData(s.nt_backend)))
        self.batt_topic.setText(s.battery_topic)
        self.key_path.setText(s.key_path)
        self.del_after.setChecked(s.delete_after_verify)
        self.match_only.setChecked(s.auto_match_logs_only)
        self.start_min.setChecked(s.start_minimized)
        self.snap_on_connect.setChecked(s.jetson_snapshot_on_connect)
        self.copy_ds.setChecked(s.copy_ds_logs)
        self.ds_dir.setText(s.ds_log_dir)
        self.ds_days.setValue(s.ds_log_days)
        self.devices = [Device.from_dict(_asdict(x)) for x in s.devices]
        self.nt_dev.setCurrentText(s.nt_device)
        self._fill_devices()
        self.nt_dev.setCurrentText(s.nt_device)
        self._check_dirty()

    # ------------------------------------------------------------ devices
    def _fill_devices(self):
        self.table.setRowCount(len(self.devices))
        for r, d in enumerate(self.devices):
            hosts = ", ".join(h.host + (f" via {h.via}" if h.via else "") for h in d.hosts)
            for c, v in enumerate([d.name, d.kind, d.user, hosts, "yes" if d.auto else ""]):
                self.table.setItem(r, c, QTableWidgetItem(v))
        # tall enough to show every device (at least 2 rows) without scrolling
        rows = max(2, len(self.devices))
        h = self.table.horizontalHeader().sizeHint().height() + rows * self.table.verticalHeader().defaultSectionSize()
        self.table.setMinimumHeight(h + 2 * self.table.frameWidth() + 4)
        cur = self.nt_dev.currentText() or self.ctx.settings.nt_device
        self.nt_dev.clear()
        self.nt_dev.addItems([d.name for d in self.devices])
        self.nt_dev.setCurrentText(cur)
        self._check_dirty()

    def _selected(self) -> Device | None:
        r = self.table.currentRow()
        return self.devices[r] if 0 <= r < len(self.devices) else None

    def add_device(self):
        dlg = DeviceDialog(None, [d.name for d in self.devices], self)
        if dlg.exec():
            nd = dlg.device()
            if not nd.name or any(d.name == nd.name for d in self.devices):
                QMessageBox.warning(self, "Device", "Give the device a unique name.")
                return
            self.devices.append(nd)
            self._fill_devices()

    def edit_device(self):
        d = self._selected()
        if d is None:
            return
        dlg = DeviceDialog(d, [x.name for x in self.devices], self)
        if dlg.exec():
            self.devices[self.devices.index(d)] = dlg.device()
            self._fill_devices()

    def remove_device(self):
        d = self._selected()
        if d and QMessageBox.question(self, "Remove", f"Remove {d.name}?") == QMessageBox.StandardButton.Yes:
            self.devices.remove(d)
            self._fill_devices()

    def _saved_device(self) -> Device | None:
        """Actions run against saved settings, so ask to save first if changed."""
        d = self._selected()
        if d is None:
            QMessageBox.information(self, "Device", "Select a device first.")
            return None
        self.save(quiet=True)
        return self.ctx.settings.device(d.name)

    def _gate_ok(self) -> bool:
        g = self.ctx.gate(manual=True, transfer=False)
        if not g.allowed:
            QMessageBox.warning(self, "Not allowed now", g.reason)
            return False
        return True

    def test_device(self):
        d = self._saved_device()
        if d is None or not self._gate_ok():
            return
        Job(lambda job: sshconn.test_key(d, self.ctx.settings)).start(
            lambda msg: (self.ctx.log(f"{d.name}: {msg}"), QMessageBox.information(self, d.name, msg)),
            lambda err: QMessageBox.warning(
                self, d.name, err + "\n\nIf this is the first time, use 'Set up key (password once)…'."
            ),
        )

    def setup_key(self):
        d = self._saved_device()
        if d is None or not self._gate_ok():
            return
        pw, ok = QInputDialog.getText(
            self,
            f"Set up key on {d.name}",
            f"Password for {d.user} on {d.name}.\nUsed once to install this laptop's key; it is NOT saved.",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not pw:
            return
        Job(lambda job: sshconn.install_key(d, self.ctx.settings, pw)).start(
            lambda msg: (self.ctx.log(f"{d.name}: key installed. {msg}"), QMessageBox.information(self, d.name, msg)),
            lambda err: QMessageBox.warning(self, d.name, err),
        )

    def forget_host(self):
        d = self._saved_device()
        if d is None:
            return
        n = sum(sshconn.forget_host_key(h.host) for h in d.hosts)
        self.ctx.log(f"{d.name}: forgot {n} stored host key(s)")
        QMessageBox.information(self, d.name, f"Removed {n} stored host key(s). The next connection trusts the new key.")

    def show_pubkey(self):
        try:
            path, pub = sshconn.ensure_key(self.key_path.text())
        except Exception as e:
            QMessageBox.warning(self, "Key", str(e))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Public key ({path}.pub)")
        lay = QVBoxLayout(dlg)
        t = QPlainTextEdit(pub)
        t.setReadOnly(True)
        lay.addWidget(t)
        dlg.resize(700, 140)
        dlg.exec()

    # ------------------------------------------------------------ save
    def save(self, quiet: bool = False):
        s = self.ctx.settings
        old_nt = (s.nt_device, s.nt_port, [_asdict(d) for d in s.devices])
        s.save_folder = self.save_folder.text().strip()
        s.pit_config = self.pit_config.text().strip()
        s.poll_seconds = self.poll.value()
        s.speed_limit_kbps = self.speed.value()
        s.in_use_seconds = self.in_use.value()
        s.delete_after_verify = self.del_after.isChecked()
        s.auto_match_logs_only = self.match_only.isChecked()
        s.start_minimized = self.start_min.isChecked()
        s.jetson_snapshot_on_connect = self.snap_on_connect.isChecked()
        s.copy_ds_logs = self.copy_ds.isChecked()
        s.ds_log_dir = self.ds_dir.text().strip()
        s.ds_log_days = self.ds_days.value()
        s.stream_sharpness_min = self.sharp.value()
        s.nt_device = self.nt_dev.currentText()
        new_backend = self.nt_backend.currentData()
        if new_backend != s.nt_backend and not quiet:
            QMessageBox.information(self, "Restart needed", "The NetworkTables client changes after you restart HOTWire.")
        s.nt_backend = new_backend
        s.key_path = self.key_path.text().strip()
        if self.batt_topic.text().strip() != s.battery_topic:
            s.battery_topic = self.batt_topic.text().strip()
            self.ctx.subscribe_battery()
        s.devices = [Device.from_dict(_asdict(d)) for d in self.devices]
        s.save()
        if old_nt != (s.nt_device, s.nt_port, [_asdict(d) for d in s.devices]):
            self.ctx.restart_nt()
        self.ctx.settings_changed.emit()
        self._saved_state = self._form_state()
        self._check_dirty()
        if not quiet:
            self.ctx.log("Settings saved")


def _asdict(d: Device) -> dict:
    from dataclasses import asdict

    return asdict(d)
