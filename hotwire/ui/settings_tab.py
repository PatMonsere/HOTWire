"""Settings tab: folders, safety options, and the device list with key setup."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
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
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        s = ctx.settings

        g = QGroupBox("General")
        f = QFormLayout(g)
        self.save_folder = QLineEdit(s.save_folder)
        f.addRow("Save logs to:", _path_row(self.save_folder, self, folder=True))
        f.addRow("", QLabel("Tip: pick a folder inside the team's Google Drive (Drive for Desktop) to share logs."))
        self.pit_config = QLineEdit(s.pit_config)
        self.pit_config.setPlaceholderText("(default: <save folder>/pit_checks.yaml)")
        f.addRow("Pit checks file:", _path_row(self.pit_config, self, folder=False, filt="YAML (*.yaml *.yml)"))
        self.poll = QSpinBox()
        self.poll.setRange(1, 600)
        self.poll.setValue(s.poll_seconds)
        f.addRow("Check every (s):", self.poll)
        self.speed = QSpinBox()
        self.speed.setRange(0, 1_000_000)
        self.speed.setValue(s.speed_limit_kbps)
        self.speed.setSuffix(" KB/s  (0 = no limit)")
        f.addRow("Download speed limit:", self.speed)
        self.in_use = QSpinBox()
        self.in_use.setRange(5, 3600)
        self.in_use.setValue(s.in_use_seconds)
        f.addRow("File 'in use' if modified within (s):", self.in_use)
        self.del_after = QCheckBox("Delete robot files after a verified download")
        self.del_after.setChecked(s.delete_after_verify)
        f.addRow("", self.del_after)
        self.tray = QCheckBox("Closing the window keeps HOTWire running in the tray")
        self.tray.setChecked(s.minimize_to_tray)
        f.addRow("", self.tray)
        self.start_min = QCheckBox("Start minimized")
        self.start_min.setChecked(s.start_minimized)
        f.addRow("", self.start_min)
        self.snap_on_connect = QCheckBox("Take a Jetson snapshot automatically when the robot connects")
        self.snap_on_connect.setChecked(s.jetson_snapshot_on_connect)
        f.addRow("", self.snap_on_connect)
        self.lock_procs = QLineEdit(", ".join(s.lock_when_processes))
        self.lock_procs.setPlaceholderText("e.g. DriverStation.exe   (empty = off)")
        f.addRow("Competition lock while running:", self.lock_procs)
        self.sharp = QDoubleSpinBox()
        self.sharp.setRange(0, 100000)
        self.sharp.setValue(s.stream_sharpness_min)
        self.sharp.setSuffix("  (0 = don't judge)")
        f.addRow("Camera sharpness minimum:", self.sharp)
        self.nt_dev = QComboBox()
        f.addRow("NetworkTables server device:", self.nt_dev)
        self.nt_backend = QComboBox()
        self.nt_backend.addItem("Auto (pyntcore if it loads, else built-in)", "auto")
        self.nt_backend.addItem("pyntcore (official WPILib library)", "pyntcore")
        self.nt_backend.addItem("Built-in (pure Python, works with Smart App Control)", "builtin")
        self.nt_backend.setCurrentIndex(max(0, self.nt_backend.findData(s.nt_backend)))
        f.addRow("NetworkTables client:", self.nt_backend)
        f.addRow("", QLabel(f"In use now: {ctx.nt_backend_desc}  (changes apply after restarting HOTWire)"))
        self.batt_topic = QLineEdit(s.battery_topic)
        self.batt_topic.setPlaceholderText("(empty = don't show)")
        f.addRow("Battery voltage NT topic:", self.batt_topic)
        self.key_path = QLineEdit(s.key_path)
        f.addRow("SSH key file:", _path_row(self.key_path, self, folder=False))

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

        lay = QVBoxLayout(self)
        lay.addWidget(g)
        lay.addWidget(d, 1)
        lay.addLayout(bottom)
        self.devices = [Device.from_dict(_asdict(x)) for x in s.devices]
        self._fill_devices()

    # ------------------------------------------------------------ devices
    def _fill_devices(self):
        self.table.setRowCount(len(self.devices))
        for r, d in enumerate(self.devices):
            hosts = ", ".join(h.host + (f" via {h.via}" if h.via else "") for h in d.hosts)
            for c, v in enumerate([d.name, d.kind, d.user, hosts, "yes" if d.auto else ""]):
                self.table.setItem(r, c, QTableWidgetItem(v))
        cur = self.nt_dev.currentText() or self.ctx.settings.nt_device
        self.nt_dev.clear()
        self.nt_dev.addItems([d.name for d in self.devices])
        self.nt_dev.setCurrentText(cur)

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
        s.minimize_to_tray = self.tray.isChecked()
        s.start_minimized = self.start_min.isChecked()
        s.jetson_snapshot_on_connect = self.snap_on_connect.isChecked()
        s.lock_when_processes = [p.strip() for p in self.lock_procs.text().split(",") if p.strip()]
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
        if not quiet:
            self.ctx.log("Settings saved")


def _asdict(d: Device) -> dict:
    from dataclasses import asdict

    return asdict(d)
